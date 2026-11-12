"""Prepare a top-up before charging; retain the accounting lease until commit."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database.crud.subscription import _lock_subscription_row
from app.database.models import Subscription
from app.services.subscription_service import SubscriptionService
from app.services.traffic_accounting import AccountingConflict


logger = structlog.get_logger(__name__)


async def add_subscription_traffic(
    db: AsyncSession,
    subscription: Subscription,
    gb: int,
    *,
    panel=None,
    commit: bool = True,
) -> Subscription:
    from app.services.traffic_accounting import AccountingConflict
    from app.services.traffic_accounting_service import grant_purchase_locked

    if not subscription.traffic_limit_gb:
        return subscription

    managed = getattr(subscription, 'traffic_accounting_mode', 'legacy') in ('managed', 'migrating')
    if managed and panel is None:
        raise AccountingConflict('Managed top-up requires a locked fresh panel snapshot')
    if panel is not None and gb <= 0:
        raise AccountingConflict('Traffic allowance must be positive')
    # Lock subscription row — защита от lost-update гонки с housekeeping в extend_subscription
    # (см. _apply_base_limit_preserving_active_purchases / _housekeep_expired_purchases).
    # Без lock'а одновременный renewal + topup могут затереть друг друга.
    await _lock_subscription_row(db, subscription)
    await db.refresh(subscription, ['traffic_accounting_mode'])
    if panel is None and subscription.traffic_accounting_mode in ('managed', 'migrating'):
        raise AccountingConflict('Managed top-up requires a locked fresh panel snapshot')

    subscription.add_traffic(gb)
    subscription.updated_at = datetime.now(UTC)

    # Создаём новую запись докупки с индивидуальной датой истечения (30 дней)
    from app.database.models import TrafficPurchase

    new_expires_at = datetime.now(UTC) + timedelta(days=30)
    new_purchase = TrafficPurchase(subscription_id=subscription.id, traffic_gb=gb, expires_at=new_expires_at)
    db.add(new_purchase)

    # Обновляем общий счетчик докупленного трафика
    current_purchased = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    subscription.purchased_traffic_gb = current_purchased + gb

    # Устанавливаем traffic_reset_at на ближайшую дату истечения из всех активных докупок
    now = datetime.now(UTC)
    active_purchases_query = (
        select(TrafficPurchase)
        .where(TrafficPurchase.subscription_id == subscription.id)
        .where(TrafficPurchase.expires_at > now)
    )
    active_purchases_result = await db.execute(active_purchases_query)
    active_purchases = active_purchases_result.scalars().all()

    if active_purchases:
        # Добавляем только что созданную покупку к списку
        all_active = list(active_purchases) + [new_purchase]
        earliest_expiry = min(p.expires_at for p in all_active)
        subscription.traffic_reset_at = earliest_expiry
    else:
        # Первая докупка
        subscription.traffic_reset_at = new_expires_at

    await db.flush()
    if panel is not None:
        account = await grant_purchase_locked(db, subscription, new_purchase, panel)
        if (
            subscription.status == 'limited'
            and subscription.user.status == 'active'
            and subscription.end_date > datetime.now(UTC)
            and account.remaining_bytes(datetime.now(UTC)) > 0
        ):
            subscription.status = 'active'
        from app.services.traffic_accounting_service import schedule_projection_after_commit

        schedule_projection_after_commit(db, subscription.id)
    if commit:
        await db.commit()
        await db.refresh(subscription)

    logger.info(
        '📈 К подписке пользователя добавлено ГБ трафика (истекает )',
        user_id=subscription.user_id,
        gb=gb,
        new_expires_at=new_expires_at.strftime('%d.%m.%Y'),
    )

    # В классическом режиме докупленный трафик входит в цену продления
    # (_calculate_classic_mode передаёт purchased_traffic_gb) — привязка с
    # прежней суммой её больше не покрывает. В тарифном режиме цена не
    # меняется, и хелпер молча выйдет по совпадению сумм.
    from app.services.recurrent_amount import sync_recurrent_bindings_after_price_change

    if commit:
        await sync_recurrent_bindings_after_price_change(db, subscription.id)

    return subscription


async def prepare_traffic_purchase(db, subscription, *, requested_gb=None):
    """Call before any monetary mutation or row locks in the caller transaction.

    Migration owns separate durable transactions. The returned subscription is
    reloaded under User → grace → Subscription locks; the snapshot is fetched
    after those locks and may only be used before the caller commits/rolls back.
    """
    from app.database.models import Tariff
    from app.services.traffic_accounting_service import (
        _fresh_panel,
        ensure_managed_account,
        lock_accounting_subscription,
    )

    def subscription_terms(sub):
        return tuple(
            getattr(sub, key)
            for key in (
                'tariff_id',
                'traffic_limit_gb',
                'purchased_traffic_gb',
                'end_date',
                'status',
                'is_trial',
            )
        )

    def tariff_terms(tariff):
        if tariff is None:
            return None
        return deepcopy(
            tuple(
                getattr(tariff, key)
                for key in (
                    'traffic_limit_gb',
                    'traffic_topup_enabled',
                    'allow_traffic_topup',
                    'traffic_topup_packages',
                    'max_topup_traffic_gb',
                )
            )
        )

    initial_terms = subscription_terms(subscription)
    tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
    initial_tariff_terms = tariff_terms(tariff)
    subscription_id = subscription.id
    owner_id = subscription.user_id
    if not subscription.traffic_limit_gb:
        raise AccountingConflict('Cannot grant finite traffic to an unlimited subscription')
    async with SubscriptionService().get_api_client() as api:
        await ensure_managed_account(
            async_sessionmaker(db.bind, expire_on_commit=False),
            api,
            subscription_id,
            timezone=getattr(settings, 'REMNAWAVE_TRAFFIC_RESET_TIMEZONE', 'UTC'),
            allow_limited_grace=True,
        )
        subscription, grace_open = await lock_accounting_subscription(db, subscription_id)
        if (
            subscription_terms(subscription) != initial_terms
            or tariff_terms(subscription.tariff) != initial_tariff_terms
        ):
            raise AccountingConflict('Subscription terms changed; retry the purchase')
        if subscription.user_id != owner_id:
            raise AccountingConflict('Subscription ownership changed')
        if grace_open:
            from app.services.traffic_accounting_service import require_limited_grace_topup

            await require_limited_grace_topup(db, subscription)
        if not subscription.traffic_limit_gb:
            raise AccountingConflict('Subscription became unlimited')
        if requested_gb is not None and requested_gb > 0 and subscription.tariff is not None:
            tariff = subscription.tariff
            if settings.is_tariffs_mode() and not tariff.can_topup_traffic():
                raise AccountingConflict('Traffic top-up is disabled for this tariff')
            maximum = tariff.max_topup_traffic_gb or 0
            if maximum and subscription.traffic_limit_gb + requested_gb > maximum:
                raise AccountingConflict('Traffic top-up exceeds the tariff limit')
        panel = await _fresh_panel(db, api, subscription)
    return subscription, panel


async def finish_traffic_purchase(db, subscription, transaction=None):
    """Run best-effort hooks only after the atomic purchase has committed."""
    from app.database.crud.transaction import emit_transaction_side_effects
    from app.database.models import TransactionType
    from app.services.recurrent_amount import sync_recurrent_bindings_after_price_change

    if transaction is not None:
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=abs(transaction.amount_kopeks),
            user_id=transaction.user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            description=transaction.description,
        )
    await sync_recurrent_bindings_after_price_change(db, subscription.id)


async def prepare_user_tariff_switch(db, subscription):
    """Settle packaged traffic before a user switch mutates its nominal rights.

    Legacy unlimited/no-package accounts keep their established switch policy;
    deciding that branch still takes the accounting lease to serialize migration.
    """
    from datetime import UTC, datetime

    from sqlalchemy import exists, select

    from app.database.models import TrafficPurchase
    from app.services.cispay_recurrent_guard import require_no_cispay
    from app.services.paid_traffic_reset_service import prepare_admin_tariff_accounting
    from app.services.traffic_accounting_service import reconcile_account_locked

    await require_no_cispay(db, user_id=subscription.user_id, subscription_id=subscription.id)

    mode = getattr(subscription, 'traffic_accounting_mode', None) or 'legacy'
    if subscription.traffic_limit_gb > 0 and mode in ('legacy', 'migrating'):
        has_packages = await db.scalar(
            select(
                exists().where(
                    TrafficPurchase.subscription_id == subscription.id,
                    TrafficPurchase.expires_at > datetime.now(UTC),
                )
            )
        )
        if has_packages or mode == 'migrating':
            subscription, panel = await prepare_traffic_purchase(db, subscription)
            await reconcile_account_locked(db, subscription, panel)
            return subscription, panel
    return await prepare_admin_tariff_accounting(db, subscription)


async def lock_daily_accounting_subscription(db, subscription, user_id):
    """Serialize daily state decisions with migration and grace before charging."""
    from app.services.cispay_recurrent_guard import require_no_cispay
    from app.services.traffic_accounting import AccountingConflict
    from app.services.traffic_accounting_service import lock_accounting_subscription

    await require_no_cispay(db, user_id=user_id, subscription_id=subscription.id)
    subscription, grace_open = await lock_accounting_subscription(db, subscription.id)
    if subscription is None or subscription.user_id != user_id:
        raise AccountingConflict('Daily subscription ownership changed')
    if subscription.traffic_accounting_mode in ('managed', 'migrating') and grace_open:
        raise AccountingConflict('Daily resume is unavailable during grace')
    if subscription.traffic_accounting_mode == 'migrating':
        raise AccountingConflict('Daily resume awaits completed migration')
    return subscription


async def stage_daily_resume_grant(db, subscription, tariff, transaction):
    """Persist the reset decision in the same transaction as the new paid day."""
    from app.services.traffic_base_grant_service import stage_paid_base_grant
    from app.services.traffic_reset_policy import should_reset_traffic_on_daily_charge

    if subscription.traffic_accounting_mode != 'managed':
        return
    from app.services.traffic_accounting_service import stage_authorized_activation
    from app.services.traffic_accounting_sync import stage_managed_sync

    # This helper runs only after the existing authorized daily-resume decision
    # and debit. A paused daily account is intentionally DISABLED in the panel;
    # distinguish its paid resume from an unrelated top-up of a banned account.
    await stage_authorized_activation(db, subscription)
    await stage_managed_sync(db, subscription)
    if not should_reset_traffic_on_daily_charge(tariff):
        return
    operation = str(transaction.id) if transaction is not None else subscription.last_daily_charge_at.isoformat()
    await stage_paid_base_grant(db, subscription, operation_key=f'daily-resume:{operation}')
