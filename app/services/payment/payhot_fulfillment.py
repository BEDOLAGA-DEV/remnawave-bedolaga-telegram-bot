"""Durable PayHot post-credit stages; legacy commits stay inside an outer transaction.

Cart automation is not replay-safe across all legacy modes. Its durable claim is
at-most-once: an interrupted attempt needs inspection, never an automatic debit retry.
"""

import html

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import GuestPurchase, PayHotPayment, ReferralEarning, Transaction, TransactionType, User
from app.services.payment.payhot import is_refund_review


logger = structlog.get_logger(__name__)


class _StageSession(AsyncSession):
    rolled_back = False

    async def rollback(self):
        self.rolled_back = True
        await super().rollback()


async def _referral_stage(db, payment_id):
    from app.services.referral_service import process_referral_topup

    # Acceptance has committed. Release its read transaction before acquiring
    # another connection; do not retain a pool slot while waiting for ourselves.
    bind = db.bind
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    await db.rollback()
    async with engine.connect() as connection, connection.begin():
        async with _StageSession(
            bind=connection, expire_on_commit=False, join_transaction_mode='create_savepoint'
        ) as stage:
            owner_id = await stage.scalar(select(PayHotPayment.user_id).where(PayHotPayment.id == payment_id))
            if owner_id is not None:
                await stage.scalar(select(User).where(User.id == owner_id).with_for_update())
            payment = await stage.scalar(select(PayHotPayment).where(PayHotPayment.id == payment_id).with_for_update())
            if payment is None or not payment.is_paid or is_refund_review(payment):
                return
            if payment.user_id != owner_id:
                raise RuntimeError('PayHot fulfillment owner changed; retry')
            state = dict(payment.fulfillment_state or {})
            if state.get('kind') != 'topup' or state.get('referral') == 'done':
                return
            if state.get('target_id') != payment.user_id:
                raise ValueError('PayHot fulfillment owner mismatch')
            earning_filter = or_(
                ReferralEarning.referral_id == payment.user_id, ReferralEarning.user_id == payment.user_id
            )
            earning_cursor = await stage.scalar(select(func.max(ReferralEarning.id)).where(earning_filter)) or 0
            transaction_filter = (Transaction.user_id == payment.user_id) & (
                Transaction.type == TransactionType.REFERRAL_REWARD.value
            )
            transaction_cursor = await stage.scalar(select(func.max(Transaction.id)).where(transaction_filter)) or 0
            result = await process_referral_topup(stage, payment.user_id, payment.amount_kopeks, bot=None)
            if result is not True or stage.rolled_back:
                raise RuntimeError('PayHot referral stage incomplete')
            user = await stage.get(User, payment.user_id)
            if user is None:
                raise ValueError('PayHot fulfillment owner missing')
            if not user.referred_by_id:
                user.has_made_first_topup = True
            earning_ids = list(
                (
                    await stage.scalars(
                        select(ReferralEarning.id).where(earning_filter, ReferralEarning.id > earning_cursor)
                    )
                ).all()
            )
            transaction_ids = list(
                (
                    await stage.scalars(
                        select(Transaction.id).where(transaction_filter, Transaction.id > transaction_cursor)
                    )
                ).all()
            )
            payment.fulfillment_state = {
                **state,
                'referral': 'done',
                'referral_earning_ids': earning_ids,
                'referee_transaction_ids': transaction_ids,
            }
            await stage.commit()


async def _claim(db, payment_id, stage_name):
    payment = await db.scalar(
        select(PayHotPayment)
        .where(PayHotPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None or not payment.is_paid or is_refund_review(payment):
        await db.rollback()
        return None
    state = dict(payment.fulfillment_state or {})
    if state.get(stage_name) in ('claimed', 'done') or state.get('state') == 'done':
        await db.rollback()
        return None
    payment.fulfillment_state = {**state, stage_name: 'claimed', 'state': 'attention'}
    await db.commit()
    return payment


async def _complete(db, payment_id, stage_name):
    payment = await db.scalar(
        select(PayHotPayment)
        .where(PayHotPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = dict(payment.fulfillment_state or {})
    payment.fulfillment_state = {
        **state,
        stage_name: 'done',
        'state': 'attention' if is_refund_review(payment) else 'done',
    }
    await db.commit()


async def fulfill_payhot_payment(db, service, payment_id):
    payment = await db.get(PayHotPayment, payment_id, populate_existing=True)
    if payment is None or not payment.is_paid or is_refund_review(payment):
        return
    state = dict(payment.fulfillment_state or {})
    if state.get('state') == 'done':
        if state.get('kind') == 'topup':
            await _notify_once(db, service, payment_id)
        return
    if state.get('kind') == 'guest':
        from app.services.guest_purchase_service import (
            _create_nalogo_receipt_for_purchase,
            fulfill_purchase,
            notify_gift_claim_available,
        )

        purchase = await db.scalar(
            select(GuestPurchase)
            .where(GuestPurchase.id == state.get('target_id'))
            .options(selectinload(GuestPurchase.tariff), selectinload(GuestPurchase.buyer))
        )
        if purchase is None or purchase.payment_id != payment.order_id:
            raise ValueError('PayHot guest fulfillment mismatch')
        if purchase.is_gift:
            if await _claim(db, payment_id, 'guest_notification') is None:
                return
            try:
                if purchase.buyer:
                    await _create_nalogo_receipt_for_purchase(db, purchase, purchase.buyer)
            except Exception as error:
                await db.rollback()
                logger.warning(
                    'PayHot gift receipt attempt failed', local_payment_id=payment_id, error_type=type(error).__name__
                )
                purchase = await db.scalar(
                    select(GuestPurchase)
                    .where(GuestPurchase.id == state['target_id'])
                    .options(selectinload(GuestPurchase.tariff))
                )
            await notify_gift_claim_available(
                purchase, tariff_name=purchase.tariff.name if purchase.tariff else '', period_days=purchase.period_days
            )
            await _complete(db, payment_id, 'guest_notification')
        else:
            # Shared guest fulfillment is keyed by its durable purchase token.
            result = await fulfill_purchase(db, purchase.token)
            if result is None or result.status != 'delivered':
                raise RuntimeError('PayHot guest fulfillment pending')
            await _complete(db, payment_id, 'guest')
        return
    if state.get('kind') != 'topup':
        raise ValueError('PayHot fulfillment kind missing')
    await _referral_stage(db, payment_id)
    payment = await _claim(db, payment_id, 'cart')
    if payment is None:
        await _notify_once(db, service, payment_id)
        return
    from app.services.payment.common import send_cart_notification_after_topup

    user = await db.get(User, payment.user_id)
    if user is None:
        raise ValueError('PayHot cart owner missing')
    # Helper catches some failures and returns False even on success. "done"
    # means this one allowed attempt finished, not that a subscription was bought.
    await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(service, 'bot', None))
    await _complete(db, payment_id, 'cart')
    await _notify_once(db, service, payment_id)


async def _notify_once(db, service, payment_id):
    """Nonfinancial delivery is at-most-once; failure cannot rerun credits/cart."""
    from app.config import settings
    from app.database.crud.transaction import emit_transaction_side_effects
    from app.database.crud.user import get_user_by_id
    from app.database.models import PaymentMethod, Transaction, TransactionType

    payment = await db.scalar(
        select(PayHotPayment)
        .where(PayHotPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None or not payment.is_paid or is_refund_review(payment):
        await db.rollback()
        return
    state = dict(payment.fulfillment_state or {})
    if state.get('notifications') in ('claimed', 'attempted'):
        await db.rollback()
        return
    payment.fulfillment_state = {**state, 'notifications': 'claimed'}
    owner_id, amount, transaction_id, order_id = (
        payment.user_id,
        payment.amount_kopeks,
        payment.transaction_id,
        payment.order_id,
    )
    old_balance = (payment.metadata_json or {}).get('balance_change', {}).get('old_balance', 0)
    await db.commit()

    async def effects():
        transaction = await db.get(Transaction, transaction_id) if transaction_id else None
        if transaction is not None:
            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=amount,
                user_id=owner_id,
                type=TransactionType.DEPOSIT,
                payment_method=PaymentMethod.PAYHOT,
                external_id=order_id,
            )

    async def notify():
        user = await get_user_by_id(db, owner_id)
        sender = getattr(service, '_send_payment_success_notification', None)
        if sender and user and user.telegram_id:
            await sender(user.telegram_id, amount, user, db=db, payment_method_title=settings.get_payhot_display_name())

    async def admin():
        if not getattr(service, 'bot', None) or not transaction_id:
            return
        from app.services.admin_notification_service import AdminNotificationService
        from app.utils.user_utils import format_referrer_info

        user = await get_user_by_id(db, owner_id)
        transaction = await db.get(Transaction, transaction_id)
        if user and transaction:
            await AdminNotificationService(service.bot).send_balance_topup_notification(
                user,
                transaction,
                old_balance,
                topup_status='Пополнение',
                referrer_info=format_referrer_info(user),
                db=db,
            )

    async def referral_notifications():
        await _send_referral_receipts(db, service, owner_id, state)

    for effect in (effects, notify, admin, referral_notifications):
        try:
            await effect()
        except Exception as error:
            await db.rollback()
            logger.warning(
                'PayHot notification attempt failed', local_payment_id=payment_id, error_type=type(error).__name__
            )
    payment = await db.scalar(
        select(PayHotPayment)
        .where(PayHotPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    payment.fulfillment_state = {**(payment.fulfillment_state or {}), 'notifications': 'attempted'}
    await db.commit()


async def _send_referral_receipts(db, service, payer_id, state):
    from app.config import settings
    from app.database.models import Tariff
    from app.localization.texts import get_texts
    from app.services.referral_service import send_referral_notification

    receipts = []
    for earning_id in state.get('referral_earning_ids', []):
        earning = await db.get(ReferralEarning, earning_id)
        if earning is None or payer_id not in (earning.user_id, earning.referral_id):
            continue
        tariff = await db.get(Tariff, earning.tariff_id) if earning.tariff_id else None
        receipts.append(
            (earning.user_id, earning.amount_kopeks, earning.days_granted, tariff.name if tariff else '', earning.level)
        )
    for transaction_id in state.get('referee_transaction_ids', []):
        transaction = await db.get(Transaction, transaction_id)
        if (
            transaction
            and transaction.user_id == payer_id
            and transaction.type == TransactionType.REFERRAL_REWARD.value
        ):
            receipts.append((payer_id, transaction.amount_kopeks, 0, '', 1))
    for recipient_id, amount, days, tariff_name, level in receipts:
        user = await db.get(User, recipient_id)
        if user is None or (amount <= 0 and days <= 0):
            continue
        texts = get_texts(user.language)
        reward = (
            texts.t('REFERRAL_REWARD_DAYS', '{days} дн. подписки').format(days=days)
            if days
            else settings.format_price(amount)
        )
        if days and tariff_name:
            reward += f' ({html.escape(tariff_name)})'
        message = texts.t(
            'REFERRAL_GIFT_UI_006', '🎉 <b>Бонус по реферальной программе!</b>\n\n🎁 Начислено: {value1}'
        ).format(value1=reward)
        try:
            await send_referral_notification(
                getattr(service, 'bot', None),
                user.telegram_id,
                message,
                user=user,
                bonus_kopeks=amount,
                bonus_days=days,
                tariff_name=tariff_name,
                level=level,
            )
        except Exception as error:
            logger.warning(
                'PayHot referral notification attempt failed',
                recipient_id=recipient_id,
                error_type=type(error).__name__,
            )
