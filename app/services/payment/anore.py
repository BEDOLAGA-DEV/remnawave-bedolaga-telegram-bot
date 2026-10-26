"""Anore: durable invoice acceptance, atomic credit, then best-effort delivery."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.anore import create_anore_payment as create_record
from app.database.crud.transaction import create_transaction
from app.database.models import AnorePayment, GuestPurchase, PaymentMethod, TransactionType, User
from app.services.anore_service import AnoreAPIError, anore_service, parse_rubles_to_kopeks


logger = structlog.get_logger(__name__)


class AnoreValidationError(ValueError):
    """Permanent rejection; contains no provider secrets or raw response."""


def _validate_remote(payment: AnorePayment, data: dict, *, require_order: bool) -> str:
    remote_id = data.get('id')
    if not isinstance(remote_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', remote_id):
        raise AnoreValidationError('Invalid invoice ID')
    if payment.anore_payment_id and remote_id != payment.anore_payment_id:
        raise AnoreValidationError('Invoice ID mismatch')
    if (require_order or 'orderId' in data) and data.get('orderId') != payment.order_id:
        raise AnoreValidationError('Order mismatch')
    if not isinstance(data.get('currency'), str) or data['currency'].upper() != payment.currency:
        raise AnoreValidationError('Currency mismatch')
    try:
        if parse_rubles_to_kopeks(data.get('amount')) != payment.amount_kopeks:
            raise AnoreValidationError('Amount mismatch')
        if 'rubAmount' in data and parse_rubles_to_kopeks(data['rubAmount']) != payment.amount_kopeks:
            raise AnoreValidationError('Ruble amount mismatch')
    except ValueError as exc:
        raise AnoreValidationError('Invalid amount') from exc
    if data.get('status') not in ('new', 'paid', 'expired'):
        raise AnoreValidationError('Invalid status')
    if 'paid' in data and (type(data['paid']) is not bool or data['paid'] != (data['status'] == 'paid')):
        raise AnoreValidationError('Conflicting paid status')
    return remote_id


def _status(payment: AnorePayment) -> dict:
    return {'payment': payment, 'status': payment.status, 'is_paid': payment.is_paid}


class AnorePaymentMixin:
    async def create_anore_payment(
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
        if not settings.is_anore_enabled() or type(amount_kopeks) is not int:
            return None
        config = anore_service.snapshot()
        # Capture operation settings before the first await (DB overrides can change).
        success_url = return_url or settings.ANORE_SUCCESS_URL
        failure_url = fail_url or settings.ANORE_FAIL_URL or success_url
        callback_url = (
            f'{settings.WEBHOOK_URL.rstrip("/")}/{settings.ANORE_WEBHOOK_PATH.lstrip("/")}'
            if settings.WEBHOOK_URL
            else None
        )
        from app.services.payment_method_config_service import get_config_by_method_id, get_enabled_methods_for_user

        user = await db.get(User, user_id) if user_id is not None else None
        if user_id is not None and (user is None or user.status == 'deleted' or user.restriction_topup):
            return None
        method_config = await get_config_by_method_id(db, 'anore')
        methods = list(config.methods)
        minimum, maximum = config.min_amount_kopeks, config.max_amount_kopeks
        if method_config is not None:
            allowed = next(
                (
                    item
                    for item in await get_enabled_methods_for_user(
                        db, user, is_first_topup=not user.has_made_first_topup if user else None
                    )
                    if item['id'] == 'anore'
                ),
                None,
            )
            if not allowed:
                return None
            # Guests cannot prove membership/type/first-topup restrictions.
            if user is None and (
                method_config.user_type_filter != 'all'
                or method_config.first_topup_filter != 'any'
                or method_config.promo_group_filter_mode == 'selected'
            ):
                return None
            minimum, maximum = allowed['min_amount_kopeks'], allowed['max_amount_kopeks']
            enabled_options = {option['id'] for option in (allowed['options'] or [])}
            methods = [method for method in methods if method in enabled_options]
        if not methods or not minimum <= amount_kopeks <= min(maximum, 2_147_483_647):
            return None
        if payment_method_type is not None:
            if payment_method_type not in methods:
                return None
            methods = [payment_method_type]
        local = await create_record(
            db,
            user_id=user_id,
            order_id=f'an_{uuid4().hex}',
            amount_kopeks=amount_kopeks,
            currency='RUB',
            description=description,
            status='new',
            is_paid=False,
            is_test=config.is_test,
            processed_events=[],
            payment_method=payment_method_type,
            metadata_json={'language': language, **(metadata or {})},
        )
        # create_record commits any pending GuestPurchase in this same session too.
        local_id = local.id
        try:
            remote = await anore_service.create_payment(
                amount_kopeks=amount_kopeks,
                description=description,
                order_id=local.order_id,
                methods=methods,
                email=email,
                success_url=success_url,
                fail_url=failure_url,
                callback_url=callback_url,
                config=config,
            )
            local = await db.scalar(
                select(AnorePayment)
                .where(AnorePayment.id == local_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if local is None:
                raise AnoreValidationError('Invoice removed during creation')
            remote_id = _validate_remote(local, remote, require_order=True)
            payment_url = remote.get('paymentUrl')
            if not isinstance(payment_url, str) or len(payment_url) > 4096:
                raise AnoreValidationError('Invalid payment URL')
            parsed = urlsplit(payment_url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise AnoreValidationError('Invalid payment URL')
            local.anore_payment_id = remote_id
            local.payment_url = payment_url
            expires = remote.get('expiresIn', 14400)
            if type(expires) is int and 0 < expires <= 31_536_000:
                local.expires_at = datetime.now(UTC) + timedelta(seconds=expires)
            # Never replace early webhook acceptance with the create response.
            await db.commit()
            return {
                'local_payment_id': local.id,
                'order_id': local.order_id,
                'payment_id': local.anore_payment_id,
                'payment_url': payment_url,
                'status': local.status,
                'amount_kopeks': amount_kopeks,
                'currency': 'RUB',
            }
        except (AnoreAPIError, ValueError):
            await db.rollback()
            logger.warning('Anore invoice creation unresolved', local_payment_id=local_id)
            return None

    async def process_anore_callback(self, db: AsyncSession, payload: dict) -> bool:
        event = payload.get('event')
        if payload.get('_event_header', event) != event:
            raise AnoreValidationError('Event mismatch')
        if event in ('payout.created', 'payout.processing', 'payout.succeeded', 'payout.failed'):
            return True
        expected = {'payment.succeeded': 'paid', 'payment.expired': 'expired'}.get(event)
        if expected is None or payload.get('status') != expected:
            raise AnoreValidationError('Event/status mismatch')
        return await self._accept_anore_state(db, payload, require_order=True)

    async def _accept_anore_state(self, db: AsyncSession, data: dict, *, require_order: bool) -> bool:
        followup = None
        try:
            payment = await db.scalar(select(AnorePayment).where(AnorePayment.order_id == data.get('orderId')))
            if payment is None:
                raise AnoreValidationError('Unknown order')
            # Account merge locks users before provider rows. Follow the same order.
            owner_id = payment.user_id
            metadata = dict(payment.metadata_json or {})
            guest = metadata.get('purpose') == 'guest_purchase' or 'purchase_token' in metadata
            user = None
            if not guest and not payment.is_test and owner_id is not None:
                user = await db.scalar(
                    select(User).where(User.id == owner_id).with_for_update().execution_options(populate_existing=True)
                )
            payment = await db.scalar(
                select(AnorePayment)
                .where(AnorePayment.id == payment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if payment.user_id != owner_id:
                raise RuntimeError('Invoice owner changed; retry acceptance')
            remote_id = _validate_remote(payment, data, require_order=require_order)
            payment.anore_payment_id = remote_id
            delivery = data.get('_delivery_id')
            events = list(payment.processed_events or [])
            if delivery and delivery not in events:
                events.append(delivery)
                payment.processed_events = events[-128:]
            # Store validated financial fields only, not arbitrary provider PII.
            payment.callback_payload = {
                key: data[key]
                for key in ('id', 'orderId', 'amount', 'rubAmount', 'currency', 'status', 'event')
                if key in data
            }
            if not payment.is_paid and data['status'] == 'paid':
                now = datetime.now(UTC)
                if not payment.is_test:
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
                            or purchase.payment_method
                            not in ('anore', 'anore_sbp', 'anore_card', 'anore_yoomoney', 'anore_crypto')
                            or (purchase.payment_id and purchase.payment_id != payment.order_id)
                        ):
                            raise AnoreValidationError('Guest purchase mismatch')
                        if purchase.status in ('pending', 'expired'):
                            purchase.status = 'paid'
                            purchase.payment_id = payment.order_id
                            purchase.paid_at = now
                            followup = ('guest', purchase.id, payment.id, None)
                    else:
                        if user is None or user.status == 'deleted':
                            raise AnoreValidationError('Invoice owner missing')
                        old_balance = user.balance_kopeks
                        transaction = await create_transaction(
                            db,
                            user_id=user.id,
                            type=TransactionType.DEPOSIT,
                            amount_kopeks=payment.amount_kopeks,
                            description=f'Пополнение через {settings.get_anore_display_name()}',
                            payment_method=PaymentMethod.ANORE,
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
                payment.status = 'paid'
                payment.is_paid = True
                payment.paid_at = now
            elif not payment.is_paid:
                payment.status = data['status']
            payment.updated_at = datetime.now(UTC)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        if followup:
            try:
                await self._after_anore_payment(db, *followup)
            except Exception:
                await db.rollback()
                logger.warning('Anore payment accepted; delivery needs retry', local_payment_id=followup[2])
        return True

    async def check_anore_payment_status(self, db: AsyncSession, order_id: str) -> dict | None:
        payment = await db.scalar(
            select(AnorePayment)
            .where(AnorePayment.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None:
            await db.rollback()
            return None
        now = datetime.now(UTC)
        if payment.is_paid or not payment.anore_payment_id or (payment.next_check_at and payment.next_check_at > now):
            await db.commit()
            return _status(payment)
        payment.last_checked_at = now
        payment.next_check_at = now + timedelta(seconds=3)
        remote_id = payment.anore_payment_id
        local_id = payment.id
        await db.commit()  # The polling reservation must survive network/processing failure.
        try:
            remote = await anore_service.get_payment(remote_id)
            # GET does not document orderId; bind to our already-known provider ID.
            _validate_remote(payment, remote, require_order=False)
            await self._accept_anore_state(db, {**remote, 'orderId': order_id}, require_order=True)
        except AnoreAPIError as exc:
            payment = await db.scalar(
                select(AnorePayment)
                .where(AnorePayment.id == local_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if payment is not None and exc.retry_after:
                now = datetime.now(UTC)
                # Keep even extreme delays finite without scheduling earlier than requested.
                seconds = min(exc.retry_after, int((datetime.max.replace(tzinfo=UTC) - now).total_seconds()) - 1)
                payment.next_check_at = max(payment.next_check_at, now + timedelta(seconds=seconds))
            await db.commit()
        except AnoreValidationError:
            await db.rollback()
            logger.warning('Anore status rejected', local_payment_id=local_id)
        payment = await db.get(AnorePayment, local_id, populate_existing=True)
        return _status(payment) if payment else None

    async def _after_anore_payment(self, db, kind, target_id, payment_id, old_balance):
        """All external effects happen after durable financial acceptance."""
        payment = await db.get(AnorePayment, payment_id)
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
                payment_method=PaymentMethod.ANORE,
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
                fresh.telegram_id, amount, fresh, db=db, payment_method_title=settings.get_anore_display_name()
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
            fresh_payment = await db.get(AnorePayment, payment_id)
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
                logger.warning('Anore post-payment effect failed', effect=effect.__name__, local_payment_id=payment_id)
