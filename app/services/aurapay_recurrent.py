"""Idempotent processing of AuraPay subscription events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    AuraPaySubscription,
    CasheraSubscription,
    LavaSubscription,
    PaymentMethod,
    PlategaSubscription,
    Subscription,
    Transaction,
    TransactionType,
)
from app.services.aurapay_service import AuraPayAPIError, aurapay_service


LIVE = ('NEW', 'WAITING_PAYMENT', 'ACTIVE')


def resolve_interval(days: int) -> tuple[int, str]:
    if days == 365:
        return 1, 'year'
    if days in (30, 90, 180):
        return days // 30, 'month'
    return days, 'day'


async def get_active(db: AsyncSession, subscription_id: int) -> AuraPaySubscription | None:
    return (
        await db.execute(
            select(AuraPaySubscription).where(
                AuraPaySubscription.subscription_id == subscription_id,
                AuraPaySubscription.status.in_(LIVE),
            )
        )
    ).scalar_one_or_none()


async def enable(db: AsyncSession, *, user, subscription: Subscription, tariff) -> AuraPaySubscription:
    """Create a card renewal plan for an existing paid tariff."""
    if not settings.is_aurapay_enabled() or not settings.AURAPAY_RECURRENT_ENABLED:
        raise ValueError('AuraPay auto-renewal is unavailable')
    if subscription.is_trial or subscription.tariff_id != tariff.id or subscription.status in ('disabled', 'pending'):
        raise ValueError('A paid tariff subscription is required')
    if not user.telegram_id or int(user.telegram_id) <= 0:
        raise ValueError('A Telegram account is required')
    if settings.AURAPAY_CURRENCY != 'RUB':
        raise ValueError('AuraPay subscriptions require RUB')

    # Serialize creation per tariff before contacting the provider.
    locked_id = (
        await db.execute(select(Subscription.id).where(Subscription.id == subscription.id).with_for_update())
    ).scalar_one_or_none()
    if locked_id is None:
        raise ValueError('Subscription not found')
    existing = await get_active(db, subscription.id)
    if existing:
        return existing
    for model in (PlategaSubscription, LavaSubscription, CasheraSubscription):
        other = (
            await db.execute(
                select(model.id).where(
                    model.subscription_id == subscription.id,
                    model.status.in_(('PENDING', 'ACTIVE', 'PAST_DUE')),
                )
            )
        ).scalar_one_or_none()
        if other is not None:
            raise ValueError('Another renewal plan is already active')

    from app.services.autopay_period import resolve_autopay_period_candidate
    from app.services.pricing_engine import pricing_engine

    charge_days = (
        resolve_autopay_period_candidate(subscription.autopay_period_days, tariff)
        or resolve_autopay_period_candidate(settings.DEFAULT_AUTOPAY_PERIOD_DAYS, tariff)
        or tariff.get_shortest_period()
    )
    if not charge_days or charge_days < 1:
        raise ValueError('Tariff has no renewal period')
    price = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        charge_days,
        device_limit=subscription.device_limit,
    )
    amount_kopeks = int(price.final_total or 0)
    if not 100 <= amount_kopeks <= 5_000_000:
        raise ValueError('Renewal amount must be between 1 and 50,000 RUB')

    period, interval = resolve_interval(charge_days)
    merchant_id = f'ap-sub-{uuid.uuid4().hex}'
    payload = {
        'subscription_id': merchant_id,
        'order_id': f'ap-first-{uuid.uuid4().hex}',
        'amount': float(Decimal(amount_kopeks) / 100),
        'period': period,
        'interval': interval,
        'description': 'Автопродление подписки',
        'service': 'card',
        'identifier': str(user.telegram_id),
        'identifier_type': 'TG_CHAT_ID',
    }
    if settings.WEBHOOK_URL:
        payload['callback_url'] = f'{settings.WEBHOOK_URL.rstrip("/")}{settings.AURAPAY_WEBHOOK_PATH}'
    if settings.AURAPAY_RETURN_URL:
        payload['success_url'] = settings.AURAPAY_RETURN_URL
        payload['fail_url'] = settings.AURAPAY_RETURN_URL
    response = await aurapay_service.create_subscription(payload)
    remote = response.get('subscription') or {}
    url = (response.get('payment_data') or {}).get('url')
    if not remote.get('id') or not url:
        raise ValueError('AuraPay did not return subscription details')

    record = AuraPaySubscription(
        user_id=user.id,
        subscription_id=subscription.id,
        merchant_id=merchant_id,
        provider_id=remote['id'],
        amount_kopeks=amount_kopeks,
        charge_days=charge_days,
        period=period,
        interval=interval,
        status='NEW',
        redirect_url=url,
    )
    db.add(record)
    subscription.autopay_enabled = False
    await db.commit()
    await db.refresh(record)
    return record


async def purchase(db: AsyncSession, *, user, tariff) -> AuraPaySubscription:
    """Start a paid tariff through the first card authorization."""
    if not settings.is_aurapay_recurrent_enabled():
        raise ValueError('AuraPay auto-renewal is unavailable')
    from app.database.crud.subscription import (
        create_sbp_pending_subscription,
        get_subscription_by_user_and_tariff,
        get_subscription_by_user_id,
    )

    if settings.is_multi_tariff_enabled():
        subscription = await get_subscription_by_user_and_tariff(db, user.id, tariff.id, include_inactive=True)
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
        if subscription is not None and subscription.tariff_id != tariff.id:
            raise ValueError('A different tariff is already subscribed')
    if subscription is not None and (subscription.is_trial or subscription.status in ('disabled', 'pending')):
        raise ValueError('This subscription cannot be paid through AuraPay auto-renewal')
    if subscription is None:
        subscription = await create_sbp_pending_subscription(db, user.id, tariff)
    return await enable(db, user=user, subscription=subscription, tariff=tariff)


async def cancel(db: AsyncSession, record: AuraPaySubscription, *, commit: bool = True) -> None:
    if record.status in ('DEACTIVATED', 'ERROR'):
        return
    try:
        await aurapay_service.cancel_subscription(record.merchant_id)
    except AuraPayAPIError as error:
        if error.status_code != 409:
            raise
        remote = await aurapay_service.get_subscription(record.merchant_id)
        remote_status = (remote.get('subscription') or remote).get('status', '')
        if str(remote_status).upper() not in ('NEW', 'DEACTIVATED', 'ERROR'):
            raise
    record.status = 'DEACTIVATED'
    if commit:
        await db.commit()
    else:
        await db.flush()


async def cancel_for_subscription(
    db: AsyncSession,
    subscription_id: int,
    *,
    commit: bool = True,
) -> None:
    record = await get_active(db, subscription_id)
    if record is not None:
        await cancel(db, record, commit=commit)


async def process_event(db: AsyncSession, payload: dict) -> bool:
    """Apply a verified webhook; each paid invoice extends the tariff once."""
    event = payload.get('event')
    merchant_id = payload.get('subscription_id')
    if event not in ('ACTIVATED', 'PAID', 'PAYMENT_ERROR', 'DEACTIVATED') or not merchant_id:
        return False
    record = (
        await db.execute(
            select(AuraPaySubscription)
            .where(AuraPaySubscription.merchant_id == merchant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if record is None or str(payload.get('shop_id')) != settings.AURAPAY_SHOP_ID:
        return False
    if str(payload.get('id')) != record.provider_id:
        return False
    if event in ('PAYMENT_ERROR', 'DEACTIVATED'):
        if event == 'DEACTIVATED':
            record.status = 'DEACTIVATED'
        elif record.status != 'DEACTIVATED':
            record.status = 'WAITING_PAYMENT'
        await db.commit()
        return True

    invoice_id = payload.get('invoice_id')
    if not invoice_id or str(payload.get('status', '')).upper() != 'ACTIVE':
        return False
    try:
        amount = Decimal(str(payload.get('amount'))) * 100
        if not amount.is_finite() or amount != amount.to_integral_value():
            return False
        received = int(amount)
    except (InvalidOperation, TypeError, ValueError):
        return False
    if received != record.amount_kopeks:
        return False
    if (
        await db.execute(
            select(Transaction.id).where(
                Transaction.external_id == str(invoice_id),
                Transaction.payment_method == PaymentMethod.AURAPAY.value,
            )
        )
    ).scalar_one_or_none() is not None:
        if record.status == 'DEACTIVATED':
            await aurapay_service.cancel_subscription(record.merchant_id)
        return True

    subscription = await db.get(Subscription, record.subscription_id, with_for_update=True)
    if subscription is None:
        return False
    from app.database.crud.subscription import reconcile_tariff_traffic_limit
    from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
    from app.services.grace_access_echo import undo_grace_overlay_echo

    await undo_grace_overlay_echo(db, subscription)
    subscription.extend_subscription(record.charge_days)
    await reconcile_tariff_traffic_limit(db, subscription)
    transaction = await create_transaction(
        db,
        user_id=record.user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=record.amount_kopeks,
        description='Автопродление AuraPay',
        payment_method=PaymentMethod.AURAPAY,
        external_id=str(invoice_id),
        commit=False,
    )
    if record.status != 'DEACTIVATED':
        record.status = 'ACTIVE'
    value = payload.get('next_pay_at')
    if value:
        try:
            next_charge_at = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if next_charge_at.tzinfo is None:
                next_charge_at = next_charge_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            next_charge_at = None
        if next_charge_at is not None:
            record.next_charge_at = next_charge_at.astimezone(UTC)
    await db.commit()
    await emit_transaction_side_effects(
        db,
        transaction,
        amount_kopeks=record.amount_kopeks,
        user_id=record.user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        payment_method=PaymentMethod.AURAPAY,
        external_id=str(invoice_id),
        description='Автопродление AuraPay',
    )
    if record.status == 'DEACTIVATED':
        # Ошибка отмены должна вызвать повтор вебхука; повторная обработка
        # платежа не продлит подписку ещё раз благодаря проверке invoice_id.
        await aurapay_service.cancel_subscription(record.merchant_id)
    try:
        from app.services.subscription_service import SubscriptionService

        await SubscriptionService().update_remnawave_user(
            db,
            subscription,
            reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
            reset_reason='Автопродление AuraPay',
        )
    except Exception:
        # The payment has already committed. Panel reconciliation can retry.
        pass
    return True
