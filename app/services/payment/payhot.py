"""PayHot durable invoice creation and atomic, canonical payment acceptance."""

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.crud.payhot import create_payhot_payment as create_record
from app.database.crud.transaction import create_transaction
from app.database.models import GuestPurchase, PayHotPayment, PaymentMethod, TransactionType, User
from app.services.payhot_service import METHODS, PayHotAPIError, _https_url, payhot_service


logger = structlog.get_logger(__name__)
STATUSES = frozenset(
    (
        'requires_payment_method',
        'requires_action',
        'processing',
        'authorized',
        'partially_captured',
        'succeeded',
        'failed',
        'cancelled',
        'refunded',
        'partially_refunded',
        'manual_reconciliation',
    )
)


def is_refund_review(payment):
    return (
        payment.status in ('refunded', 'partially_refunded')
        or (payment.fulfillment_state or {}).get('reason') == 'refund_review'
    )


def _validate_remote(payment, data):
    if not isinstance(data, dict):
        raise ValueError('Invalid PayHot payment')
    # Recurring charges use payment.* events too, but must be reconciled
    # against their mandate/cycle receipt, never an ordinary balance invoice.
    if data.get('origin') == 'recurring' or any(
        data.get(key) is not None for key in ('recurring_charge_id', 'merchant_recurring_charge_reference')
    ):
        raise ValueError('PayHot recurring charge is not an ordinary payment')
    remote_id = data.get('id')
    try:
        if not isinstance(remote_id, str) or str(UUID(remote_id)) != remote_id:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Invalid PayHot identifier') from None
    if payment.payhot_payment_id and remote_id != payment.payhot_payment_id:
        raise ValueError('PayHot identifier mismatch')
    if data.get('merchant_order_reference') != payment.order_id:
        raise ValueError('PayHot order mismatch')
    if data.get('currency') != 'RUB' or data.get('amount_minor') != str(payment.amount_kopeks):
        raise ValueError('PayHot principal mismatch')
    if data.get('payment_method') not in (
        {'card', 'applepay', 'googlepay'} if payment.payment_method == 'card' else {payment.payment_method}
    ):
        raise ValueError('PayHot method mismatch')
    if data.get('status') not in STATUSES:
        raise ValueError('Invalid PayHot status')
    environment = data.get('environment')
    if environment is not None and environment != 'production':
        raise ValueError('PayHot environment mismatch')
    fees = data.get('fees')
    if fees is not None and (not isinstance(fees, dict) or fees.get('principal_minor') != str(payment.amount_kopeks)):
        raise ValueError('PayHot fee principal mismatch')
    return remote_id


def _result(payment):
    return {
        'local_payment_id': payment.id,
        'order_id': payment.order_id,
        'payment_id': payment.payhot_payment_id,
        'payment_url': payment.payment_url,
        'status': payment.status,
        'amount_kopeks': payment.amount_kopeks,
        'currency': payment.currency,
    }


def _status(payment):
    return {'payment': payment, 'status': payment.status, 'is_paid': payment.is_paid}


class PayHotPaymentMixin:
    async def create_payhot_payment(
        self,
        db,
        *,
        user_id,
        amount_kopeks,
        description='Пополнение баланса',
        email=None,
        language='ru',
        payment_method_type=None,
        return_url=None,
        fail_url=None,
        metadata=None,
        payment_phone=None,
    ):
        from app.services.payment_method_config_service import get_payhot_checkout_policy

        if type(amount_kopeks) is not int or not 0 < amount_kopeks <= 2_147_483_647:
            return None
        config = payhot_service.snapshot()
        user = await db.get(User, user_id) if user_id is not None else None
        if user_id is not None and (user is None or user.status == 'deleted' or user.restriction_topup):
            return None
        payer = user
        contact_email = None
        purchase_token = (metadata or {}).get('purchase_token')
        if purchase_token:
            purchase = await db.scalar(select(GuestPurchase).where(GuestPurchase.token == purchase_token))
            if purchase is None:
                return None
            payer = await db.get(User, purchase.buyer_user_id) if purchase.buyer_user_id else None
            if purchase.buyer_user_id and payer is None:
                return None
            if purchase.contact_type == 'email':
                contact_email = purchase.contact_value
        if payer is not None and (payer.status == 'deleted' or payer.restriction_topup):
            return None
        policy = await get_payhot_checkout_policy(db, payer)
        if not policy or not policy['min_amount_kopeks'] <= amount_kopeks <= policy['max_amount_kopeks']:
            return None
        options = policy.get('options', [])
        option_ids = [item['id'] if isinstance(item, dict) else item for item in options]
        method = payment_method_type or (option_ids[0] if option_ids else None)
        if method not in option_ids or method not in METHODS:
            return None
        if method == 'sberpay' and (
            not isinstance(payment_phone, str) or not re.fullmatch(r'\+7[0-9]{10}', payment_phone)
        ):
            return None
        order_id = f'ph_{uuid4().hex}'
        payload = {
            'merchant_order_reference': order_id,
            'amount_minor': str(amount_kopeks),
            'currency': 'RUB',
            'payment_method': method,
        }
        customer = {'id': f'user_{payer.id}' if payer is not None else order_id}
        actual_email = (email or (payer.email if payer else None) or contact_email or '').strip()
        if actual_email:
            customer['email'] = actual_email
        if method == 'sberpay':
            customer.update(phone=payment_phone, sberpay={'front_type': 'back2app'})
        payload['customer'] = customer
        for key, value in (
            ('success_return_url', return_url or settings.PAYHOT_SUCCESS_URL),
            ('failure_return_url', fail_url or settings.PAYHOT_FAIL_URL),
        ):
            if value:
                payload[key] = _https_url(value)
        local = await create_record(
            db,
            user_id=user_id,
            order_id=order_id,
            amount_kopeks=amount_kopeks,
            currency='RUB',
            description=description,
            payment_method=method,
            status='creating',
            is_paid=False,
            request_payload=payload,
            metadata_json={**(metadata or {}), 'language': language},
            processed_events=[],
            fulfillment_state={},
        )
        local_id = local.id
        try:
            await self._recover_payhot_creation(db, local, config=config)
            local = await db.get(PayHotPayment, local_id, populate_existing=True)
            return _result(local) if local and local.payment_url else None
        except (PayHotAPIError, ValueError):
            await db.rollback()
            logger.warning('PayHot creation requires recovery', local_payment_id=local_id)
            return None

    async def _recover_payhot_creation(self, db, payment, *, config):
        if (payment.metadata_json or {}).get('purpose') == 'payhot_recurring':
            from app.services.payhot_recurrent_service import create_payhot_agreement

            await create_payhot_agreement(db, payment.metadata_json['agreement_id'], bot=getattr(self, 'bot', None))
            return
        local_id = payment.id
        # Serialize recovery/reissue: a second reissue invalidates the first checkout URL.
        payment = await db.scalar(
            select(PayHotPayment)
            .where(PayHotPayment.id == local_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment.payment_url and payment.payhot_payment_id:
            await db.commit()
            return
        envelope = await payhot_service.create_payment(payment.request_payload, payment.order_id, config=config)
        data = envelope.get('payment')
        remote_id = _validate_remote(payment, data)
        if data['status'] in ('succeeded', 'refunded', 'partially_refunded'):
            payment.payhot_payment_id = remote_id
            # Canonical GET still decides acceptance; terminal invoices need no new checkout.
            if not payment.is_paid:
                payment.status = 'processing'
            await db.commit()
            return
        checkout = envelope.get('checkout') or {}
        url = checkout.get('url') or payment.payment_url
        if not url:
            reissued = await payhot_service.reissue_checkout(
                remote_id, idempotency_key=f'{payment.order_id}_{uuid4().hex}', config=config
            )
            url = (reissued.get('checkout') or reissued).get('url')
        url = _https_url(url)
        payment = await db.scalar(
            select(PayHotPayment)
            .where(PayHotPayment.id == local_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        _validate_remote(payment, data)
        payment.payhot_payment_id = remote_id
        payment.payment_url = url
        expires_at = data.get('expires_at')
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                if expiry.tzinfo is None:
                    raise ValueError
                payment.expires_at = expiry
            except (ValueError, TypeError, AttributeError):
                raise ValueError('Invalid PayHot expiry') from None
        if not payment.is_paid:
            # Creation is not proof of money: acceptance always goes through canonical GET.
            payment.status = 'processing' if data['status'] == 'succeeded' else data['status']
        await db.commit()

    async def process_payhot_callback(self, db, payload):
        data = payload.get('data')
        if not isinstance(data, dict):
            raise ValueError('Invalid PayHot event data')
        if (
            payload.get('event_type', '').startswith('recurring_charge.')
            or data.get('origin') == 'recurring'
            or any(data.get(key) is not None for key in ('recurring_charge_id', 'merchant_recurring_charge_reference'))
        ):
            from app.services.payhot_recurrent_service import process_payhot_recurring_callback

            return await process_payhot_recurring_callback(db, payload, bot=getattr(self, 'bot', None))
        payment = await db.scalar(
            select(PayHotPayment).where(PayHotPayment.order_id == data.get('merchant_order_reference'))
        )
        if payment is None:
            raise ValueError('Unknown PayHot order')
        _validate_remote(payment, data)
        event_type = payload.get('event_type', '')
        if not event_type.startswith('payment.') or payload.get('environment') not in (None, 'production'):
            raise ValueError('PayHot event environment mismatch')
        if (payment.metadata_json or {}).get('purpose') == 'payhot_recurring':
            from app.services.payhot_recurrent_service import refresh_payhot_enrollment

            await refresh_payhot_enrollment(db, payment.metadata_json['agreement_id'], bot=getattr(self, 'bot', None))
            return True
        config = payhot_service.snapshot()
        remote = await payhot_service.get_payment(data['id'], config=config)
        await self._accept_payhot_state(db, payment.id, remote.get('payment'), event_id=payload.get('event_id'))
        return True

    async def _accept_payhot_state(self, db, local_id, data, *, event_id=None):
        try:
            payment = await db.get(PayHotPayment, local_id)
            if payment is None:
                raise ValueError('Unknown PayHot order')
            if (payment.metadata_json or {}).get('purpose') == 'payhot_recurring':
                from app.services.payhot_recurrent_service import accept_enrollment_payment

                await accept_enrollment_payment(db, local_id, data, bot=getattr(self, 'bot', None))
                return
            owner_id = payment.user_id
            metadata = dict(payment.metadata_json or {})
            guest = metadata.get('purpose') == 'guest_purchase' or 'purchase_token' in metadata
            user = None
            if owner_id is not None and not guest:
                user = await db.scalar(
                    select(User).where(User.id == owner_id).with_for_update().execution_options(populate_existing=True)
                )
            payment = await db.scalar(
                select(PayHotPayment)
                .where(PayHotPayment.id == local_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if payment.user_id != owner_id:
                raise RuntimeError('PayHot owner changed; retry')
            payment.payhot_payment_id = _validate_remote(payment, data)
            now = datetime.now(UTC)
            refund_review = is_refund_review(payment) or data['status'] in ('refunded', 'partially_refunded')
            if refund_review:
                # A verified refund is a durable fence, not permission to undo
                # an already booked balance or to grant again on a stale success.
                if data['status'] in ('refunded', 'partially_refunded') and payment.status != 'refunded':
                    payment.status = data['status']
                payment.fulfillment_state = {
                    **(payment.fulfillment_state or {}),
                    'state': 'attention',
                    'reason': 'refund_review',
                }
            elif not payment.is_paid and data['status'] == 'succeeded':
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
                        or purchase.payment_method not in {'payhot', *(f'payhot_{method}' for method in METHODS)}
                        or (purchase.payment_id and purchase.payment_id != payment.order_id)
                    ):
                        raise ValueError('PayHot guest purchase mismatch')
                    if purchase.status in ('pending', 'expired'):
                        purchase.status, purchase.payment_id, purchase.paid_at = 'paid', payment.order_id, now
                    target_id = purchase.id
                else:
                    if user is None or user.status == 'deleted':
                        raise ValueError('PayHot owner missing')
                    old_balance = user.balance_kopeks
                    transaction = await create_transaction(
                        db,
                        user_id=user.id,
                        type=TransactionType.DEPOSIT,
                        amount_kopeks=payment.amount_kopeks,
                        description=f'Пополнение через {settings.get_payhot_display_name()}',
                        payment_method=PaymentMethod.PAYHOT,
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
                    target_id = user.id
                payment.fulfillment_state = {
                    'kind': 'guest' if guest else 'topup',
                    'target_id': target_id,
                    'state': 'pending',
                }
                payment.is_paid, payment.paid_at = True, now
            if not refund_review and (not payment.is_paid or data['status'] == 'succeeded'):
                payment.status = data['status']
            payment.callback_payload = {
                key: data[key]
                for key in ('id', 'merchant_order_reference', 'amount_minor', 'currency', 'payment_method', 'status')
                if key in data
            }
            if event_id:
                payment.processed_events = list(dict.fromkeys([*(payment.processed_events or []), event_id]))[-128:]
            payment.updated_at = now
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        if payment.is_paid and not refund_review:
            await self._fulfill_payhot(db, local_id)

    async def _fulfill_payhot(self, db, local_id):
        from app.services.payment.payhot_fulfillment import fulfill_payhot_payment

        try:
            payment = await db.get(PayHotPayment, local_id)
            if payment and (payment.metadata_json or {}).get('purpose') == 'payhot_recurring':
                from app.services.payhot_recurrent_service import deliver_payhot_renewal

                await deliver_payhot_renewal(db, payment.metadata_json['agreement_id'], payment_id=local_id)
                return
            if payment is None or is_refund_review(payment):
                return
            await fulfill_payhot_payment(db, self, local_id)
        except Exception:
            await db.rollback()
            logger.warning('PayHot accepted; fulfillment pending', local_payment_id=local_id)

    async def check_payhot_payment_status(self, db, local_id):
        payment = await db.scalar(
            select(PayHotPayment)
            .where(PayHotPayment.id == local_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None:
            await db.rollback()
            return None
        if payment.is_paid:
            await db.commit()
            await self._fulfill_payhot(db, local_id)
            return _status(await db.get(PayHotPayment, local_id, populate_existing=True))
        now = datetime.now(UTC)
        if payment.next_check_at and payment.next_check_at > now:
            await db.commit()
            return _status(payment)
        payment.last_checked_at, payment.next_check_at = now, now + timedelta(seconds=30)
        config = payhot_service.snapshot()
        await db.commit()
        try:
            if (payment.metadata_json or {}).get('purpose') == 'payhot_recurring':
                from app.services.payhot_recurrent_service import refresh_payhot_enrollment

                await refresh_payhot_enrollment(
                    db, payment.metadata_json['agreement_id'], bot=getattr(self, 'bot', None)
                )
                return _status(await db.get(PayHotPayment, local_id, populate_existing=True))
            if not payment.payhot_payment_id:
                await self._recover_payhot_creation(db, payment, config=config)
                payment = await db.get(PayHotPayment, local_id, populate_existing=True)
            remote = await payhot_service.get_payment(payment.payhot_payment_id, config=config)
            await self._accept_payhot_state(db, local_id, remote.get('payment'))
        except PayHotAPIError as exc:
            await db.rollback()
            if exc.retry_after:
                payment = await db.get(PayHotPayment, local_id, populate_existing=True)
                payment.next_check_at = datetime.now(UTC) + timedelta(seconds=min(exc.retry_after, 86400 * 365))
                await db.commit()
        except ValueError:
            await db.rollback()
            logger.warning('PayHot canonical payment rejected', local_payment_id=local_id)
        payment = await db.get(PayHotPayment, local_id, populate_existing=True)
        return _status(payment) if payment else None
