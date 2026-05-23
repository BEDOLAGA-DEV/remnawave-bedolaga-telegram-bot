"""Trial → Paid auto-conversion orchestrator.

Находит trial-подписки с активной saved card и autopay_enabled, и инициирует
рекуррентное списание через :func:`get_provider` (provider abstraction layer).

Поток:
    1. ``process_trial_conversions`` (cron-tick) — выбирает кандидатов,
       вызывает ``RecurringProvider.charge`` для каждого. Возвращает stats.
    2. Реальное списание подтверждается **асинхронно** через webhook провайдера.
       Webhook handler по идемпотентному ``payment_id`` с префиксом
       :data:`TRIAL_CONVERT_PAYMENT_PREFIX` вызывает
       :meth:`SubscriptionService.convert_trial_to_paid`, которая атомарно
       конвертирует sub: ``is_trial=False``, ``end_date += period_days``,
       создаёт audit ``Transaction`` и синхронизирует RemnaWave panel.
    3. ``convert_trial_to_paid_from_callback`` — entrypoint для webhook'ов.

Идемпотентность: ``payment_id = trial_convert_<sub_id>_<YYYYMMDD>``. EtoPlatezhi
требует уникальный ``payment_id`` per project — один tick = один charge per
sub per day. Повторный tick того же дня → провайдер вернёт duplicate.

Multi-provider: какой провайдер использовать — определяется по
``saved_payment_methods.provider``. Поэтому одни и те же оркестратор работает
с EtoPlatezhi/YooKassa/будущими адаптерами без изменений.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    SavedPaymentMethod,
    Subscription,
    SubscriptionStatus,
    User,
)


logger = structlog.get_logger(__name__)

TRIAL_CONVERT_PAYMENT_PREFIX = 'trial_convert_'


def build_idempotency_key(subscription_id: int, now: datetime | None = None) -> str:
    """``trial_convert_<sub_id>_<YYYYMMDD>`` — один charge per sub per day."""
    if now is None:
        now = datetime.now(UTC)
    return f'{TRIAL_CONVERT_PAYMENT_PREFIX}{subscription_id}_{now.strftime("%Y%m%d")}'


def parse_subscription_id(payment_id: str) -> int | None:
    """Extract subscription_id from ``trial_convert_<sub_id>_<date>``."""
    if not payment_id or not payment_id.startswith(TRIAL_CONVERT_PAYMENT_PREFIX):
        return None
    rest = payment_id[len(TRIAL_CONVERT_PAYMENT_PREFIX):]
    parts = rest.split('_', 1)
    if not parts or not parts[0].isdigit():
        return None
    return int(parts[0])


async def process_trial_conversions(db: AsyncSession) -> dict:
    """Cron tick: списать с saved card для триалов на грани истечения.

    Реальная конверсия (is_trial=False, +period_days) случится в webhook
    после успешного списания провайдером.
    """
    try:
        from app.services.payment.recurring import enabled_providers, get_provider
    except ImportError as e:
        logger.warning('trial_conversion: provider abstraction not available', error=str(e))
        return {'skipped': True, 'reason': 'no_provider_abstraction'}

    if not enabled_providers():
        return {'skipped': True, 'reason': 'no_enabled_providers'}

    stats = {
        'checked': 0,
        'charge_requested': 0,
        'no_card': 0,
        'provider_disabled': 0,
        'no_tariff_price': 0,
        'charge_failed': 0,
        'errors': 0,
    }

    try:
        subs = await _find_trials_for_conversion(db)
        stats['checked'] = len(subs)

        # Snapshot ids + re-fetch per iteration. После flush/savepoint в
        # _process_single_trial ORM атрибуты на старых объектах могут стать
        # stale (MissingGreenlet при попытке lazy load). Тот же fix применён
        # в recurrent_payment_service.
        subscription_ids = [sub.id for sub in subs]
        for sub_id in subscription_ids:
            subscription = await _reload_subscription_with_relations(db, sub_id)
            if not subscription:
                continue
            try:
                outcome = await _process_single_trial(db, subscription, get_provider)
                stats[outcome] = stats.get(outcome, 0) + 1
            except Exception as e:
                stats['errors'] += 1
                logger.error(
                    'trial_conversion: error processing subscription',
                    subscription_id=sub_id,
                    error=str(e),
                    exc_info=True,
                )
    except Exception as e:
        logger.error('trial_conversion: outer error', error=str(e), exc_info=True)
        stats['errors'] += 1

    if stats['charge_requested'] > 0 or stats['errors'] > 0 or stats['charge_failed'] > 0:
        logger.info('trial_conversion: tick stats', **stats)

    return stats


_TRIAL_CONVERSION_BATCH_LIMIT = 500


async def _find_trials_for_conversion(db: AsyncSession) -> list[Subscription]:
    """Триалы с autopay_enabled, истекающие в ближайшие 12ч.

    Окно T-12h..T+0 от end_date. 1 попытка списания в день (idempotency_key
    per-day). Если карта декларнула — триал кончится, юзер увидит "продлите"
    в кабинете.

    Каждый tick обрабатывает максимум :data:`_TRIAL_CONVERSION_BATCH_LIMIT`
    подписок чтобы monitoring cycle не блокировался при массовой promo-волне
    триалов в одном окне. Остаток подхватит следующий tick.
    """
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=12)

    q = (
        select(Subscription)
        .where(
            and_(
                Subscription.is_trial == True,
                Subscription.autopay_enabled == True,
                Subscription.status.in_([
                    SubscriptionStatus.ACTIVE.value,
                    SubscriptionStatus.TRIAL.value,
                ]),
                Subscription.end_date >= now,
                Subscription.end_date <= horizon,
            )
        )
        .order_by(Subscription.end_date.asc())
        .limit(_TRIAL_CONVERSION_BATCH_LIMIT)
    )
    result = await db.execute(q)
    return list(result.scalars().all())


async def _reload_subscription_with_relations(
    db: AsyncSession, subscription_id: int
) -> Subscription | None:
    """Eager-load sub + user + tariff. Защита от MissingGreenlet после
    flush'ей внутри _process_single_trial."""
    q = (
        select(Subscription)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )
        .where(Subscription.id == subscription_id)
    )
    return (await db.execute(q)).scalar_one_or_none()


async def _process_single_trial(db: AsyncSession, subscription: Subscription, get_provider_fn) -> str:
    """Returns one of:
    'charge_requested' | 'no_card' | 'provider_disabled' | 'no_tariff_price' | 'charge_failed'
    """
    user = subscription.user
    tariff = subscription.tariff

    if not tariff:
        return 'no_tariff_price'

    # Цена месячного (самого короткого) периода
    period_days = tariff.get_shortest_period() if hasattr(tariff, 'get_shortest_period') else 30
    period_days = period_days or 30
    amount_kopeks = None
    if hasattr(tariff, 'get_price_for_period'):
        amount_kopeks = tariff.get_price_for_period(period_days)
    if not amount_kopeks or amount_kopeks <= 0:
        return 'no_tariff_price'

    # Saved card (берём самую свежую активную)
    saved = await _get_active_saved_card(db, user.id)
    if not saved:
        return 'no_card'

    provider = get_provider_fn(saved.provider)
    if not provider or not provider.is_enabled():
        return 'provider_disabled'

    idempotency_key = build_idempotency_key(subscription.id)
    metadata = {
        'user_id': user.id,
        'user_telegram_id': user.telegram_id,
        'subscription_id': subscription.id,
        'tariff_id': tariff.id,
        'period_days': period_days,
        'purpose': 'trial_conversion',
    }
    description = f'Конверсия триала в подписку ({period_days} дн.)'

    logger.info(
        'trial_conversion: requesting charge',
        subscription_id=subscription.id,
        user_id=user.id,
        provider=saved.provider,
        provider_token_prefix=str(saved.provider_token)[:8],
        amount_kopeks=amount_kopeks,
        idempotency_key=idempotency_key,
    )

    try:
        result = await provider.charge(
            provider_token=saved.provider_token,
            amount_kopeks=amount_kopeks,
            description=description,
            metadata=metadata,
            idempotency_key=idempotency_key,
            user_id=user.id,
        )
    except Exception as e:
        logger.error(
            'trial_conversion: provider.charge raised',
            subscription_id=subscription.id,
            user_id=user.id,
            provider=saved.provider,
            error=str(e),
            exc_info=True,
        )
        return 'charge_failed'

    if not result.success:
        logger.warning(
            'trial_conversion: charge declined',
            subscription_id=subscription.id,
            user_id=user.id,
            provider=saved.provider,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        return 'charge_failed'

    logger.info(
        'trial_conversion: charge request accepted by provider',
        subscription_id=subscription.id,
        user_id=user.id,
        provider=saved.provider,
        provider_payment_id=result.provider_payment_id,
    )
    return 'charge_requested'


async def _get_active_saved_card(db: AsyncSession, user_id: int) -> SavedPaymentMethod | None:
    q = (
        select(SavedPaymentMethod)
        .where(
            and_(
                SavedPaymentMethod.user_id == user_id,
                SavedPaymentMethod.is_active == True,
                SavedPaymentMethod.provider_token.isnot(None),
            )
        )
        .order_by(SavedPaymentMethod.created_at.desc())
        .limit(1)
    )
    return (await db.execute(q)).scalar_one_or_none()


async def convert_trial_to_paid_from_callback(
    db: AsyncSession,
    *,
    subscription_id: int,
    amount_kopeks: int | None,
    provider: str,
    provider_payment_id: str | None,
    period_days: int | None = None,
) -> bool:
    """Webhook entrypoint: provider confirmed successful settlement.

    Caller обязан commit'ить транзакцию после возврата True.
    """
    from app.services.subscription_service import SubscriptionService

    q = (
        select(Subscription)
        .options(selectinload(Subscription.tariff))
        .where(Subscription.id == subscription_id)
    )
    sub = (await db.execute(q)).scalar_one_or_none()
    if not sub:
        logger.warning(
            'convert_trial_to_paid_from_callback: subscription not found',
            subscription_id=subscription_id,
        )
        return False

    if not sub.is_trial:
        logger.info(
            'convert_trial_to_paid_from_callback: already paid, noop',
            subscription_id=subscription_id,
        )
        return True

    if period_days is None:
        tariff = sub.tariff
        if tariff and hasattr(tariff, 'get_shortest_period'):
            period_days = tariff.get_shortest_period() or 30
        else:
            period_days = 30

    service = SubscriptionService()
    updated = await service.convert_trial_to_paid(
        db,
        sub,
        period_days=period_days,
        amount_kopeks=amount_kopeks,
        provider=provider,
        provider_payment_id=provider_payment_id,
    )
    return updated is not None
