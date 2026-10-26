"""CisPay hosted payments: validated provenance and atomic financial acceptance."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.cispay import create_cispay_payment as create_record
from app.database.crud.transaction import create_transaction
from app.database.models import CisPayPayment, GuestPurchase, PaymentMethod, TransactionType, User
from app.services.cispay_service import CisPayAPIError, cispay_service


logger = structlog.get_logger(__name__)

CISPAY_STATUS_MAP = {
    'PENDING': ('pending', False),
    'PAID': ('success', True),
    'FAILED': ('declined', False),
    'EXPIRED': ('expired', False),
    'REFUNDED': ('refunded', False),
}
CISPAY_METHOD_MAP = {'sbp': 'SBP', 'card': 'CARD', 'crypto': 'CRYPTO'}


class CisPayValidationError(ValueError):
    """Permanent rejection of provider data; message contains no sensitive payload."""


def resolve_cispay_method(payment_method_type: str | None) -> str:
    if payment_method_type is not None:
        method = CISPAY_METHOD_MAP.get(payment_method_type.lower())
        if method is None:
            raise CisPayValidationError('Unsupported payment method')
        return method
    if settings.is_cispay_sbp_enabled() and not settings.is_cispay_card_enabled():
        return 'SBP'
    return 'CARD'


def _positive_int(value) -> int:
    if type(value) is not int or value <= 0:
        raise CisPayValidationError('Invalid amount')
    return value


def _validate_remote(payment, data, *, require_store: bool, create: bool = False, allow_missing_source: bool = False):
    remote_id = data.get('id')
    if not isinstance(remote_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', remote_id):
        raise CisPayValidationError('Invalid payment ID')
    if payment.cispay_payment_id and remote_id != payment.cispay_payment_id:
        raise CisPayValidationError('Payment ID mismatch')
    if data.get('order_id') != payment.order_id:
        raise CisPayValidationError('Order mismatch')
    expected_shop = (payment.metadata_json or {}).get('store_id') or settings.CISPAY_SHOP_ID
    if (require_store or 'store_id' in data) and data.get('store_id') != expected_shop:
        raise CisPayValidationError('Store mismatch')
    if (
        data.get('event')
        or data.get('object') == 'subscription'
        or data.get('subscription_id')
        or data.get('is_subscription')
    ):
        raise CisPayValidationError('Recurring payment is not supported')
    if data.get('status') not in CISPAY_STATUS_MAP:
        raise CisPayValidationError('Invalid status')
    if 'is_sandbox' in data and type(data['is_sandbox']) is not bool:
        raise CisPayValidationError('Invalid sandbox flag')
    if data.get('block_reason') is not None and not isinstance(data['block_reason'], str):
        raise CisPayValidationError('Invalid block reason')
    method = payment.payment_method
    if (not create or 'payment_method' in data) and data.get('payment_method') != method:
        raise CisPayValidationError('Payment method mismatch')
    amount = _positive_int(data.get('amount'))
    if method == 'CRYPTO':
        if (not create or 'currency' in data) and data.get('currency') != 'USD':
            raise CisPayValidationError('Crypto currency mismatch')
        if not allow_missing_source or data.get('source_amount') is not None or data.get('source_currency') is not None:
            if (
                data.get('source_currency') != 'RUB'
                or _positive_int(data.get('source_amount')) != payment.amount_kopeks
            ):
                raise CisPayValidationError('Crypto source mismatch')
    else:
        if (not create or 'currency' in data) and data.get('currency') != payment.currency:
            raise CisPayValidationError('Currency mismatch')
        if amount != payment.amount_kopeks:
            raise CisPayValidationError('Amount mismatch')
    return remote_id


def _set_expiry(payment, data):
    seconds = data.get('expires_in_seconds')
    if type(seconds) is int and 0 <= seconds <= 31_536_000:
        payment.expires_at = datetime.now(UTC) + timedelta(seconds=seconds)


def _status(payment):
    return {
        'payment': payment,
        'status': payment.status,
        'is_paid': bool(payment.is_paid and not payment.is_sandbox),
        'is_sandbox': payment.is_sandbox,
    }


class CisPayPaymentMixin:
    async def create_cispay_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        payment_method_type: str | None = None,
        return_url: str | None = None,
        fail_url: str | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        if not settings.is_cispay_enabled() or type(amount_kopeks) is not int or amount_kopeks <= 0:
            return None
        config = cispay_service.snapshot()
        from app.services.payment_method_config_service import get_cispay_checkout_policy

        user = await db.get(User, user_id) if user_id is not None else None
        if user_id is not None and (user is None or user.status == 'deleted' or user.restriction_topup):
            return None
        policy = await get_cispay_checkout_policy(db, user, payment_method_type)
        if not policy or not policy['selected_method']:
            return None
        method = policy['selected_method']
        try:
            capabilities = await cispay_service.get_store_capabilities(config=config)
            if capabilities.get('store_id') != config.shop_id or capabilities.get('is_active') is not True:
                return None
            active_methods = {
                item.get('payment_method')
                for item in capabilities.get('payment_methods', [])
                if isinstance(item, dict) and item.get('is_active') is True
            }
            if method not in active_methods:
                if payment_method_type is not None:
                    return None
                allowed_methods = {option['id'].upper() for option in policy['options']}
                method = next(
                    (candidate for candidate in ('CARD', 'SBP') if candidate in active_methods & allowed_methods), None
                )
                if method is None:
                    return None
                policy = await get_cispay_checkout_policy(db, user, method.lower())
                if not policy or not policy['min_amount_kopeks'] <= amount_kopeks <= min(
                    policy['max_amount_kopeks'], 2_147_483_647
                ):
                    return None
        except CisPayAPIError:
            return None
        if not policy['min_amount_kopeks'] <= amount_kopeks <= min(policy['max_amount_kopeks'], 2_147_483_647):
            return None
        if method == 'CARD' and amount_kopeks < 5000:
            return None
        local = await create_record(
            db,
            user_id=user_id,
            order_id=f'cis_{uuid4().hex}',
            amount_kopeks=amount_kopeks,
            currency='RUB',
            description=description,
            payment_method=method,
            metadata_json={**(metadata or {}), 'language': language, 'store_id': config.shop_id},
        )
        # Commit includes any pending GuestPurchase in this session, before provider HTTP.
        local_id = local.id
        try:
            remote = await cispay_service.create_payment(
                amount_kopeks=amount_kopeks,
                order_id=local.order_id,
                payment_method=method,
                customer_id=(
                    str(user.telegram_id)
                    if user is not None and user.telegram_id is not None
                    else f'customer-{user_id}'
                    if user_id is not None
                    else local.order_id
                ),
                description=description[:512] if description else None,
                redirect_success_url=return_url,
                redirect_fail_url=fail_url or return_url,
                config=config,
            )
            local = await db.scalar(
                select(CisPayPayment)
                .where(CisPayPayment.id == local_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if local is None:
                raise CisPayValidationError('Invoice removed during creation')
            local.cispay_payment_id = _validate_remote(local, remote, require_store=False, create=True)
            payment_url = remote.get('payment_url')
            if not isinstance(payment_url, str) or len(payment_url) > 4096:
                raise CisPayValidationError('Invalid payment URL')
            parsed = urlsplit(payment_url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise CisPayValidationError('Invalid payment URL')
            local.payment_url = payment_url
            if method != 'CRYPTO' and remote.get('charged_amount') is not None:
                local.charged_amount_kopeks = _positive_int(remote['charged_amount'])
            local.is_sandbox = local.is_sandbox or remote.get('is_sandbox', False) or bool(remote.get('block_reason'))
            _set_expiry(local, remote)
            await db.commit()
            return {
                'local_payment_id': local.id,
                'order_id': local.order_id,
                'payment_id': local.cispay_payment_id,
                'payment_url': local.payment_url,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_kopeks / 100,
                'currency': 'RUB',
                'is_sandbox': local.is_sandbox,
                'expires_at': local.expires_at.isoformat() if local.expires_at else None,
            }
        except (CisPayAPIError, ValueError):
            await db.rollback()
            logger.warning('CisPay invoice creation unresolved', local_payment_id=local_id)
            return None

    async def process_cispay_callback(self, db: AsyncSession, payload: dict) -> bool:
        if payload.get('event'):
            if (
                payload.get('object') == 'subscription'
                and payload.get('_event_header', payload['event']) == payload['event']
                and payload.get('store_id') == settings.CISPAY_SHOP_ID
                and payload['event']
                in {
                    'subscription.activated',
                    'subscription.cancelled',
                    'subscription.failed',
                }
            ):
                return True
            raise CisPayValidationError('Invalid event')
        payment = await db.scalar(select(CisPayPayment).where(CisPayPayment.order_id == payload.get('order_id')))
        if payment is None:
            raise CisPayValidationError('Unknown order')
        try:
            _validate_remote(payment, payload, require_store=True, allow_missing_source=True)
            if payment.is_paid and payment.payment_method == 'CRYPTO' and payload.get('source_amount') is None:
                previous = payment.callback_payload or {}
                if payload['amount'] != previous.get('amount'):
                    raise CisPayValidationError('Crypto settled amount mismatch')
                payload = {
                    **payload,
                    'source_amount': previous.get('source_amount'),
                    'source_currency': previous.get('source_currency'),
                }
            needs_status = (
                not payment.is_paid
                and payload.get('status') == 'PAID'
                and (
                    'is_sandbox' not in payload
                    or (payment.payment_method == 'CRYPTO' and payload.get('source_amount') is None)
                )
            )
            if needs_status:
                remote = await self._fetch_cispay_state(db, payment.id)
                if remote is None:
                    # A reserved GET must finish; provider retries instead of acknowledging an unaccepted payment.
                    raise RuntimeError('Payment verification is pending')
                remote = {'currency': 'RUB', 'is_sandbox': False, **remote}
                _validate_remote(payment, remote, require_store=False)
                if remote['status'] != payload['status'] or remote['amount'] != payload['amount']:
                    raise CisPayValidationError('Callback and API state disagree')
                payload = {
                    **remote,
                    'store_id': payload['store_id'],
                    'is_sandbox': bool(payload.get('is_sandbox') or remote.get('is_sandbox')),
                    'block_reason': payload.get('block_reason') or remote.get('block_reason'),
                }
            return await self._accept_cispay_state(db, payload, require_store=True)
        except Exception:
            await db.rollback()
            raise

    async def _fetch_cispay_state(self, db, payment_id):
        payment = await db.scalar(
            select(CisPayPayment)
            .where(CisPayPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        if payment is None or (payment.next_check_at and payment.next_check_at > now):
            await db.commit()
            return None
        config = cispay_service.snapshot()
        if (payment.metadata_json or {}).get('store_id', config.shop_id) != config.shop_id:
            raise CisPayValidationError('Configured store differs from invoice store')
        payment.next_check_at = now + timedelta(seconds=3)
        remote_id, order_id = payment.cispay_payment_id, payment.order_id
        await db.commit()
        try:
            return await cispay_service.check_payment(
                payment_id=remote_id, order_id=None if remote_id else order_id, config=config
            )
        except CisPayAPIError as exc:
            if getattr(exc, 'retry_after', None):
                payment = await db.scalar(
                    select(CisPayPayment)
                    .where(CisPayPayment.id == payment_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if payment:
                    delay = min(
                        exc.retry_after, int((datetime.max.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()) - 1
                    )
                    payment.next_check_at = max(payment.next_check_at, datetime.now(UTC) + timedelta(seconds=delay))
                await db.commit()
            raise

    async def check_cispay_payment_status(self, db: AsyncSession, order_id: str) -> dict | None:
        payment = await db.scalar(select(CisPayPayment).where(CisPayPayment.order_id == order_id))
        if payment is None:
            return None
        local_id = payment.id
        if payment.is_paid:
            return _status(payment)
        try:
            remote = await self._fetch_cispay_state(db, local_id)
            if remote is not None:
                remote = {'currency': 'RUB', 'is_sandbox': False, **remote}
                _validate_remote(payment, remote, require_store=False)
                # Status schema defaults sandbox to false, unlike create which cannot prove it.
                await self._accept_cispay_state(db, remote, require_store=False)
        except (CisPayAPIError, CisPayValidationError):
            await db.rollback()
            logger.warning('CisPay status verification unresolved', local_payment_id=local_id)
        payment = await db.get(CisPayPayment, local_id, populate_existing=True)
        return _status(payment) if payment else None

    async def _accept_cispay_state(self, db: AsyncSession, data: dict, *, require_store: bool) -> bool:
        followup = None
        try:
            payment = await db.scalar(select(CisPayPayment).where(CisPayPayment.order_id == data.get('order_id')))
            if payment is None:
                raise CisPayValidationError('Unknown order')
            # Account merge locks users before provider rows. Follow the same order.
            owner_id = payment.user_id
            metadata = dict(payment.metadata_json or {})
            guest = metadata.get('purpose') == 'guest_purchase' or 'purchase_token' in metadata
            user = None
            if not guest and not payment.is_sandbox and owner_id is not None:
                user = await db.scalar(
                    select(User).where(User.id == owner_id).with_for_update().execution_options(populate_existing=True)
                )
            payment = await db.scalar(
                select(CisPayPayment)
                .where(CisPayPayment.id == payment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if payment.user_id != owner_id:
                raise RuntimeError('Invoice owner changed; retry acceptance')
            remote_id = _validate_remote(
                payment, data, require_store=require_store, allow_missing_source=data.get('status') != 'PAID'
            )
            payment.cispay_payment_id = remote_id
            # Store validated financial fields only, not arbitrary provider PII.
            payment.callback_payload = {
                key: data[key]
                for key in (
                    'id',
                    'order_id',
                    'amount',
                    'source_amount',
                    'source_currency',
                    'currency',
                    'status',
                    'payment_method',
                    'is_sandbox',
                )
                if key in data
            }
            incoming_sandbox = data.get('is_sandbox', False) or bool(data.get('block_reason'))
            if payment.is_paid and payment.transaction_id and incoming_sandbox:
                raise CisPayValidationError('Sandbox provenance conflicts with credited payment')
            payment.is_sandbox = payment.is_sandbox or incoming_sandbox
            _set_expiry(payment, data)
            if payment.payment_method != 'CRYPTO' and data.get('charged_amount') is not None:
                payment.charged_amount_kopeks = _positive_int(data['charged_amount'])
            if payment.status == 'refunded' and not payment.is_paid:
                await db.commit()
                return True
            if not payment.is_paid and data['status'] == 'PAID':
                now = datetime.now(UTC)
                if not payment.is_sandbox:
                    if guest:
                        purchase = await db.scalar(
                            select(GuestPurchase)
                            .where(GuestPurchase.token == metadata.get('purchase_token'))
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                        if (
                            purchase is None
                            or purchase.amount_kopeks != payment.amount_kopeks
                            or purchase.currency != 'RUB'
                            or purchase.payment_method not in ('cispay', 'cispay_sbp', 'cispay_card', 'cispay_crypto')
                            or (purchase.payment_id and purchase.payment_id != payment.order_id)
                        ):
                            raise CisPayValidationError('Guest purchase mismatch')
                        if purchase.status in ('pending', 'expired'):
                            purchase.status = 'paid'
                            purchase.payment_id = payment.order_id
                            purchase.paid_at = now
                            followup = ('guest', purchase.id, payment.id, None)
                    else:
                        if user is None or user.status == 'deleted':
                            raise CisPayValidationError('Invoice owner missing')
                        old_balance = user.balance_kopeks
                        transaction = await create_transaction(
                            db,
                            user_id=user.id,
                            type=TransactionType.DEPOSIT,
                            amount_kopeks=payment.amount_kopeks,
                            description=f'Пополнение через {settings.get_cispay_display_name()}',
                            payment_method=PaymentMethod.CISPAY,
                            external_id=payment.order_id,
                            commit=False,
                        )
                        payment.transaction_id = transaction.id
                        user.balance_kopeks += payment.amount_kopeks
                        user.updated_at = now
                        payment.metadata_json = {
                            **metadata,
                            'balance_credited': True,
                            'balance_change': {
                                'old_balance': old_balance,
                                'new_balance': user.balance_kopeks,
                                'credited_at': now.isoformat(),
                            },
                        }
                        followup = ('topup', user.id, payment.id, old_balance)
                payment.status = 'success'
                payment.is_paid = True
                payment.paid_at = now
            elif not payment.is_paid:
                payment.status = CISPAY_STATUS_MAP[data['status']][0]
            payment.updated_at = datetime.now(UTC)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        if followup:
            try:
                await self._after_cispay_payment(db, *followup)
            except Exception:
                await db.rollback()
                logger.warning('CisPay payment accepted; delivery needs retry', local_payment_id=followup[2])
        return True

    async def _after_cispay_payment(self, db, kind, target_id, payment_id, old_balance):
        """All external effects happen after durable financial acceptance."""
        payment = await db.get(CisPayPayment, payment_id)
        if kind == 'guest':
            from app.services.guest_purchase_service import (
                _create_nalogo_receipt_for_purchase,
                fulfill_purchase,
                notify_gift_claim_available,
            )

            purchase = await db.get(GuestPurchase, target_id)
            if purchase.is_gift:
                if purchase.buyer:
                    try:
                        await _create_nalogo_receipt_for_purchase(db, purchase, purchase.buyer)
                    except Exception:
                        await db.rollback()
                        purchase = await db.get(GuestPurchase, target_id)
                await notify_gift_claim_available(
                    purchase,
                    tariff_name=purchase.tariff.name if purchase.tariff else '',
                    period_days=purchase.period_days,
                )
            else:
                await fulfill_purchase(db, purchase.token)
            return
        from app.database.crud.transaction import emit_transaction_side_effects
        from app.database.models import Transaction
        from app.services.payment.common import send_cart_notification_after_topup
        from app.services.referral_service import process_referral_topup

        amount = payment.amount_kopeks
        transaction = await db.get(Transaction, payment.transaction_id)

        # A notification failure must not suppress independent referral/cart work.
        async def effects():
            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=amount,
                user_id=target_id,
                type=TransactionType.DEPOSIT,
                payment_method=PaymentMethod.CISPAY,
                external_id=payment.order_id,
            )

        async def referral():
            await process_referral_topup(db, target_id, amount, getattr(self, 'bot', None))
            fresh = await db.get(User, target_id)
            if not fresh.has_made_first_topup and not fresh.referred_by_id:
                fresh.has_made_first_topup = True
                await db.commit()

        async def notify():
            fresh = await db.get(User, target_id)
            await self._send_payment_success_notification(
                fresh.telegram_id, amount, fresh, db=db, payment_method_title=settings.get_cispay_display_name()
            )

        async def admin():
            if not getattr(self, 'bot', None):
                return
            from app.database.crud.user import get_user_by_id
            from app.services.admin_notification_service import AdminNotificationService
            from app.utils.user_utils import format_referrer_info

            fresh = await get_user_by_id(db, target_id)
            await db.refresh(fresh, attribute_names=['promo_group', 'user_promo_groups'])
            for group in fresh.user_promo_groups:
                await db.refresh(group, attribute_names=['promo_group'])
            fresh_payment = await db.get(CisPayPayment, payment_id)
            fresh_transaction = await db.get(Transaction, fresh_payment.transaction_id)
            await AdminNotificationService(self.bot).send_balance_topup_notification(
                fresh,
                fresh_transaction,
                old_balance,
                topup_status='Пополнение',
                referrer_info=format_referrer_info(fresh),
                subscription=getattr(fresh, 'subscription', None),
                promo_group=fresh.get_primary_promo_group(),
                db=db,
            )

        async def cart():
            fresh = await db.get(User, target_id)
            await send_cart_notification_after_topup(fresh, amount, db, getattr(self, 'bot', None))

        for effect in (effects, referral, admin, notify, cart):
            try:
                await effect()
            except Exception:
                await db.rollback()
                logger.warning('CisPay post-payment effect failed', effect=effect.__name__, local_payment_id=payment_id)
