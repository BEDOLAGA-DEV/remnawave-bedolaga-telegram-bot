from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError

from app.config import settings
from app.database.crud.notification import clear_notifications
from app.database.models import (
    Subscription,
    SubscriptionServer,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

_WEBHOOK_GUARD_SECONDS = 60


def is_recently_updated_by_webhook(subscription: Subscription) -> bool:
    """Return True if subscription was updated by webhook within guard window."""
    if not subscription.last_webhook_update_at:
        return False
    elapsed = (datetime.now(UTC) - subscription.last_webhook_update_at).total_seconds()
    return elapsed < _WEBHOOK_GUARD_SECONDS


def calc_device_limit_on_tariff_switch(
    current_device_limit: int | None,
    old_tariff_device_limit: int | None,
    new_tariff_device_limit: int | None,
    max_device_limit: int | None = None,
) -> int:
    """Calculate device_limit when switching tariffs.

    Resets to new tariff base device limit — previously purchased
    extra devices are NOT carried over.  Capped at max_device_limit.
    """
    new_base = new_tariff_device_limit if new_tariff_device_limit is not None else 1

    effective_max = max_device_limit or (settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None)
    if effective_max and new_base > effective_max:
        new_base = effective_max

    return new_base


def is_active_paid_subscription(subscription: Subscription | None) -> bool:
    """Return True if subscription is active, paid (non-trial), and not expired."""
    if not subscription:
        return False
    return (
        not subscription.is_trial
        and subscription.status == SubscriptionStatus.ACTIVE.value
        and subscription.end_date is not None
        and subscription.end_date > datetime.now(UTC)
    )


async def get_subscription_by_user_id(db: AsyncSession, user_id: int) -> Subscription | None:
    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()

    if subscription:
        logger.info(
            '🔍 Загружена подписка для пользователя статус',
            subscription_id=subscription.id,
            user_id=user_id,
            status=subscription.status,
        )
        subscription = await check_and_update_subscription_status(db, subscription)

    return subscription


async def create_trial_subscription(
    db: AsyncSession,
    user_id: int,
    duration_days: int = None,
    traffic_limit_gb: int = None,
    device_limit: int | None = None,
    squad_uuid: str = None,
    connected_squads: list[str] = None,
    tariff_id: int | None = None,
) -> Subscription:
    """Создает триальную подписку.

    Args:
        connected_squads: Список UUID сквадов (если указан, squad_uuid игнорируется)
        tariff_id: ID тарифа (для режима тарифов)
    """
    duration_days = duration_days or settings.TRIAL_DURATION_DAYS
    traffic_limit_gb = traffic_limit_gb or settings.TRIAL_TRAFFIC_LIMIT_GB
    if device_limit is None:
        device_limit = settings.TRIAL_DEVICE_LIMIT

    # Если переданы connected_squads, используем их
    # Иначе используем squad_uuid или получаем случайный
    final_squads = []
    if connected_squads:
        final_squads = connected_squads
    elif squad_uuid:
        final_squads = [squad_uuid]
    else:
        try:
            from app.database.crud.server_squad import get_random_trial_squad_uuid

            random_squad = await get_random_trial_squad_uuid(db)
            if random_squad:
                final_squads = [random_squad]
                logger.debug(
                    'Выбран сквад для триальной подписки пользователя', random_squad=random_squad, user_id=user_id
                )
        except Exception as error:
            logger.error('Не удалось получить сквад для триальной подписки пользователя', user_id=user_id, error=error)

    end_date = datetime.now(UTC) + timedelta(days=duration_days)

    # Check for existing PENDING trial subscription (retry after failed payment)
    existing = await get_subscription_by_user_id(db, user_id)
    if existing and existing.is_trial and existing.status == SubscriptionStatus.PENDING.value:
        existing.status = SubscriptionStatus.ACTIVE.value
        existing.start_date = datetime.now(UTC)
        existing.end_date = end_date
        existing.traffic_limit_gb = traffic_limit_gb
        existing.device_limit = device_limit
        existing.connected_squads = final_squads
        existing.tariff_id = tariff_id
        await db.commit()
        await db.refresh(existing)
        logger.info(
            '🎁 Обновлена PENDING триальная подписка для пользователя', existing_id=existing.id, user_id=user_id
        )
        return existing

    subscription = Subscription(
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=True,
        start_date=datetime.now(UTC),
        end_date=end_date,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        connected_squads=final_squads,
        autopay_enabled=settings.is_autopay_enabled_by_default(),
        autopay_days_before=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
        tariff_id=tariff_id,
    )

    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)

    logger.info(
        f'🎁 Создана триальная подписка для пользователя {user_id}' + (f' с тарифом {tariff_id}' if tariff_id else '')
    )

    if final_squads:
        try:
            from app.database.crud.server_squad import (
                add_user_to_servers,
                get_server_ids_by_uuids,
            )

            server_ids = await get_server_ids_by_uuids(db, final_squads)
            if server_ids:
                await add_user_to_servers(db, server_ids)
                logger.info('📈 Обновлен счетчик пользователей для триальных сквадов', final_squads=final_squads)
            else:
                logger.warning('⚠️ Не удалось найти серверы для обновления счетчика (сквады)', final_squads=final_squads)
        except Exception as error:
            logger.error(
                '⚠️ Ошибка обновления счетчика пользователей для триальных сквадов',
                final_squads=final_squads,
                error=error,
            )

    return subscription


async def create_paid_subscription(
    db: AsyncSession,
    user_id: int,
    duration_days: int,
    traffic_limit_gb: int = 0,
    device_limit: int | None = None,
    connected_squads: list[str] = None,
    update_server_counters: bool = False,
    is_trial: bool = False,
    tariff_id: int | None = None,
    commit: bool = True,
) -> Subscription:
    end_date = datetime.now(UTC) + timedelta(days=duration_days)

    if device_limit is None:
        device_limit = settings.DEFAULT_DEVICE_LIMIT

    # Fallback: если connected_squads пустой — берём первый доступный сквад
    final_squads = list(connected_squads or [])
    if not final_squads:
        try:
            from app.database.crud.server_squad import get_available_server_squads

            available = await get_available_server_squads(db)
            if available:
                final_squads = [available[0].squad_uuid]
                logger.warning(
                    '⚠️ connected_squads пустой при создании подписки, используем fallback сквад',
                    user_id=user_id,
                    fallback_squad=final_squads[0],
                )
        except Exception as error:
            logger.error('❌ Не удалось получить fallback сквад', user_id=user_id, error=error)

    subscription = Subscription(
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=is_trial,
        start_date=datetime.now(UTC),
        end_date=end_date,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        connected_squads=final_squads,
        autopay_enabled=settings.is_autopay_enabled_by_default(),
        autopay_days_before=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
        tariff_id=tariff_id,
    )

    db.add(subscription)
    if commit:
        await db.commit()
        await db.refresh(subscription)
    else:
        await db.flush()

    logger.info(
        '💎 Создана платная подписка для пользователя ID: статус',
        user_id=user_id,
        subscription_id=subscription.id,
        status=subscription.status,
    )

    squad_uuids = list(final_squads)
    if update_server_counters and squad_uuids:
        try:
            from app.database.crud.server_squad import (
                add_user_to_servers,
                get_server_ids_by_uuids,
            )

            server_ids = await get_server_ids_by_uuids(db, squad_uuids)
            if server_ids:
                await add_user_to_servers(db, server_ids)
                logger.info(
                    '📈 Обновлен счетчик пользователей для платной подписки пользователя (сквады:)',
                    user_id=user_id,
                    squad_uuids=squad_uuids,
                )
            else:
                logger.warning(
                    '⚠️ Не удалось найти серверы для обновления счетчика платной подписки пользователя (сквады:)',
                    user_id=user_id,
                    squad_uuids=squad_uuids,
                )
        except Exception as error:
            logger.error(
                '⚠️ Ошибка обновления счетчика пользователей серверов для платной подписки пользователя',
                user_id=user_id,
                error=error,
            )

    return subscription


async def replace_subscription(
    db: AsyncSession,
    subscription: Subscription,
    *,
    duration_days: int,
    traffic_limit_gb: int,
    device_limit: int,
    connected_squads: list[str],
    is_trial: bool,
    autopay_enabled: bool | None = None,
    autopay_days_before: int | None = None,
    update_server_counters: bool = False,
    commit: bool = True,
) -> Subscription:
    """Перезаписывает параметры существующей подписки пользователя."""

    current_time = datetime.now(UTC)
    old_squads = set(subscription.connected_squads or [])

    # Fallback: если connected_squads пустой — берём первый доступный сквад
    final_connected = list(connected_squads or [])
    if not final_connected:
        try:
            from app.database.crud.server_squad import get_available_server_squads

            available = await get_available_server_squads(db)
            if available:
                final_connected = [available[0].squad_uuid]
                logger.warning(
                    '⚠️ connected_squads пустой при замене подписки, используем fallback сквад',
                    subscription_id=subscription.id,
                    fallback_squad=final_connected[0],
                )
        except Exception as error:
            logger.error('❌ Не удалось получить fallback сквад', subscription_id=subscription.id, error=error)

    new_squads = set(final_connected)

    new_autopay_enabled = subscription.autopay_enabled if autopay_enabled is None else autopay_enabled
    new_autopay_days_before = subscription.autopay_days_before if autopay_days_before is None else autopay_days_before

    subscription.status = SubscriptionStatus.ACTIVE.value
    subscription.is_trial = is_trial
    subscription.start_date = current_time
    subscription.end_date = current_time + timedelta(days=duration_days)
    subscription.traffic_limit_gb = traffic_limit_gb
    subscription.traffic_used_gb = 0.0

    # Удаляем записи TrafficPurchase перед сбросом purchased_traffic_gb
    from app.database.models import TrafficPurchase

    await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
    subscription.purchased_traffic_gb = 0  # Сбрасываем докупленный трафик при замене подписки
    subscription.traffic_reset_at = None  # Сбрасываем дату сброса трафика
    subscription.device_limit = device_limit
    subscription.connected_squads = list(new_squads)
    subscription.subscription_url = None
    subscription.subscription_crypto_link = None
    subscription.remnawave_short_uuid = None
    subscription.autopay_enabled = new_autopay_enabled
    subscription.autopay_days_before = new_autopay_days_before
    subscription.updated_at = current_time

    if commit:
        await db.commit()
        await db.refresh(subscription)
    else:
        await db.flush()

    # Очищаем старые записи об отправленных уведомлениях при замене подписки
    # (аналогично extend_subscription), чтобы новые уведомления отправлялись корректно
    await clear_notifications(db, subscription.id, commit=commit)

    if update_server_counters:
        try:
            from app.database.crud.server_squad import (
                get_server_ids_by_uuids,
                update_server_user_counts,
            )

            squads_to_remove = old_squads - new_squads
            squads_to_add = new_squads - old_squads

            remove_ids = await get_server_ids_by_uuids(db, list(squads_to_remove)) if squads_to_remove else []
            add_ids = await get_server_ids_by_uuids(db, list(squads_to_add)) if squads_to_add else []

            if remove_ids or add_ids:
                await update_server_user_counts(
                    db,
                    add_ids=add_ids or None,
                    remove_ids=remove_ids or None,
                )

            logger.info(
                '♻️ Обновлены параметры подписки : удалено сквадов , добавлено',
                subscription_id=subscription.id,
                squads_to_remove_count=len(squads_to_remove),
                squads_to_add_count=len(squads_to_add),
            )
        except Exception as error:
            logger.error(
                '⚠️ Ошибка обновления счетчиков серверов при замене подписки',
                subscription_id=subscription.id,
                error=error,
            )

    return subscription


async def extend_subscription(
    db: AsyncSession,
    subscription: Subscription,
    days: int,
    *,
    tariff_id: int | None = None,
    traffic_limit_gb: int | None = None,
    device_limit: int | None = None,
    connected_squads: list[str] | None = None,
    commit: bool = True,
) -> Subscription:
    """Продлевает подписку на указанное количество дней.

    Args:
        db: Сессия базы данных
        subscription: Подписка для продления
        days: Количество дней для продления
        tariff_id: ID тарифа (опционально, для режима тарифов)
        traffic_limit_gb: Лимит трафика ГБ (опционально, для режима тарифов)
        device_limit: Лимит устройств (опционально, для режима тарифов)
        connected_squads: Список UUID сквадов (опционально, для режима тарифов)
    """
    from app.database.models import TrafficPurchase

    current_time = datetime.now(UTC)

    logger.info('🔄 Продление подписки на дней', subscription_id=subscription.id, days=days)
    logger.info(
        '📊 Текущие параметры: статус=, окончание=, тариф',
        status=subscription.status,
        end_date=subscription.end_date,
        tariff_id=subscription.tariff_id,
    )

    # Определяем, происходит ли СМЕНА тарифа (а не продление того же)
    # Включает переход из классического режима (tariff_id=None) в тарифный
    is_tariff_change = tariff_id is not None and (subscription.tariff_id is None or tariff_id != subscription.tariff_id)

    # Определяем, была ли подписка истёкшей ДО продления (статус меняется ниже)
    was_expired = subscription.status in (
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.LIMITED.value,
    ) or (subscription.end_date is not None and subscription.end_date <= current_time)

    if is_tariff_change:
        logger.info('🔄 Обнаружена СМЕНА тарифа: →', tariff_id=subscription.tariff_id, tariff_id_2=tariff_id)

    if days < 0:
        subscription.end_date = subscription.end_date + timedelta(days=days)
        logger.info(
            '📅 Срок подписки уменьшен на дней, новая дата окончания', abs=abs(days), end_date=subscription.end_date
        )
    elif is_tariff_change:
        # При СМЕНЕ тарифа сохраняем оставшееся время активной подписки
        # Для триалов — только если включена настройка TRIAL_ADD_REMAINING_DAYS_TO_PAID
        remaining_seconds = 0
        if subscription.end_date and subscription.end_date > current_time:
            if not subscription.is_trial or settings.TRIAL_ADD_REMAINING_DAYS_TO_PAID:
                remaining = subscription.end_date - current_time
                remaining_seconds = max(0, remaining.total_seconds())
                logger.info(
                    '🎁 Обнаружен остаток подписки, будет добавлен к новому сроку',
                    remaining_seconds=int(remaining_seconds),
                    subscription_id=subscription.id,
                    is_trial=subscription.is_trial,
                )
        subscription.end_date = current_time + timedelta(days=days, seconds=remaining_seconds)
        subscription.start_date = current_time
        logger.info(
            '📅 СМЕНА тарифа: срок начинается с текущей даты + дней + остаток',
            days=days,
            remaining_seconds=int(remaining_seconds),
        )
    elif subscription.end_date > current_time:
        # Подписка активна - просто добавляем дни к текущей дате окончания
        # БЕЗ бонусных дней (они уже учтены в end_date)
        subscription.end_date = subscription.end_date + timedelta(days=days)
        logger.info('📅 Подписка активна, добавляем дней к текущей дате окончания', days=days)
    else:
        # Подписка истекла - начинаем с текущей даты
        subscription.end_date = current_time + timedelta(days=days)
        logger.info('📅 Подписка истекла, устанавливаем новую дату окончания на дней', days=days)

    # УДАЛЕНО: Автоматическая конвертация триала по длительности
    # Теперь триал конвертируется ТОЛЬКО после успешного коммита продления
    # и ТОЛЬКО вызывающей функцией (например, _auto_extend_subscription)

    # Логируем статус подписки перед проверкой
    logger.info(
        '🔄 Продление подписки текущий статус: дни',
        subscription_id=subscription.id,
        status=subscription.status,
        days=days,
    )

    if days > 0 and subscription.status in (
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.LIMITED.value,
    ):
        previous_status = subscription.status
        subscription.status = SubscriptionStatus.ACTIVE.value
        logger.info(
            '🔄 Статус подписки изменён с на ACTIVE', subscription_id=subscription.id, previous_status=previous_status
        )
    elif days > 0 and subscription.status == SubscriptionStatus.TRIAL.value:
        subscription.status = SubscriptionStatus.ACTIVE.value
        logger.info('🔄 Статус подписки изменён с trial на ACTIVE', subscription_id=subscription.id)
    elif days > 0 and subscription.status == SubscriptionStatus.PENDING.value:
        logger.warning('⚠️ Попытка продлить PENDING подписку , дни', subscription_id=subscription.id, days=days)

    # Обновляем параметры тарифа, если переданы
    if tariff_id is not None:
        old_tariff_id = subscription.tariff_id
        subscription.tariff_id = tariff_id
        logger.info('📦 Обновлен тариф подписки: →', old_tariff_id=old_tariff_id, tariff_id=tariff_id)

        # При покупке тарифа сбрасываем триальный статус
        if subscription.is_trial:
            subscription.is_trial = False
            logger.info('🎓 Подписка конвертирована из триала в платную', subscription_id=subscription.id)

    if traffic_limit_gb is not None:
        old_traffic = subscription.traffic_limit_gb
        # Сброс использованного трафика: при смене тарифа — по настройке, при продлении — всегда
        if is_tariff_change:
            if settings.RESET_TRAFFIC_ON_TARIFF_SWITCH:
                subscription.traffic_used_gb = 0.0
        else:
            subscription.traffic_used_gb = 0.0

        if is_tariff_change or was_expired:
            # При СМЕНЕ тарифа или ИСТЁКШЕЙ подписке — сбрасываем все докупки трафика
            subscription.traffic_limit_gb = traffic_limit_gb
            await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
            subscription.purchased_traffic_gb = 0
            subscription.traffic_reset_at = None
            reason = 'смена тарифа' if is_tariff_change else 'подписка была истёкшей'
            logger.info(
                '📊 Обновлен лимит трафика: ГБ → ГБ (докупки сброшены)',
                old_traffic=old_traffic,
                traffic_limit_gb=traffic_limit_gb,
                reason=reason,
            )
        else:
            # Подписка активна, тот же тариф — сохраняем докупленный трафик
            purchased = subscription.purchased_traffic_gb or 0
            subscription.traffic_limit_gb = traffic_limit_gb + purchased
            logger.info(
                '📊 Обновлен лимит трафика: ГБ → ГБ (докупки сохранены: ГБ)',
                old_traffic=old_traffic,
                traffic_limit_gb=traffic_limit_gb + purchased,
                purchased=purchased,
            )
    elif settings.RESET_TRAFFIC_ON_PAYMENT:
        subscription.traffic_used_gb = 0.0
        if subscription.tariff_id is None or was_expired:
            # Классический режим или истёкшая подписка — сбрасываем докупки
            await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
            subscription.purchased_traffic_gb = 0
            subscription.traffic_reset_at = None
            logger.info(
                '🔄 Сбрасываем использованный и докупленный трафик',
                was_expired=was_expired,
                tariff_id=subscription.tariff_id,
            )
        else:
            # Активная подписка в режиме тарифов — сохраняем purchased_traffic_gb и traffic_reset_at
            logger.info('🔄 Сбрасываем использованный трафик, докупленный сохранен (режим тарифов)')

    if device_limit is not None:
        old_devices = subscription.device_limit
        subscription.device_limit = device_limit
        logger.info('📱 Обновлен лимит устройств: →', old_devices=old_devices, device_limit=device_limit)

    if connected_squads is not None:
        # Не перезаписываем существующие сквады пустым списком
        if connected_squads or not subscription.connected_squads:
            old_squads = subscription.connected_squads
            subscription.connected_squads = connected_squads
            logger.info('🌍 Обновлены сквады: →', old_squads=old_squads, connected_squads=connected_squads)
        else:
            logger.warning(
                '⚠️ Попытка перезаписать сквады пустым списком, сохраняем текущие',
                subscription_id=subscription.id,
                current_squads=subscription.connected_squads,
            )

    # Обработка daily полей при смене тарифа
    if is_tariff_change and tariff_id is not None:
        # Получаем информацию о новом тарифе для проверки is_daily
        from app.database.crud.tariff import get_tariff_by_id

        new_tariff = await get_tariff_by_id(db, tariff_id)
        old_was_daily = (
            getattr(subscription, 'is_daily_paused', False)
            or getattr(subscription, 'last_daily_charge_at', None) is not None
        )

        if new_tariff and getattr(new_tariff, 'is_daily', False):
            # Переход на суточный тариф - сбрасываем флаги
            subscription.is_daily_paused = False
            subscription.last_daily_charge_at = None  # Будет установлено при первом списании
            logger.info('🔄 Переход на суточный тариф: сброшены daily флаги')
        elif old_was_daily:
            # Переход с суточного на обычный тариф - очищаем daily поля
            subscription.is_daily_paused = False
            subscription.last_daily_charge_at = None
            logger.info('🔄 Переход с суточного тарифа: очищены daily флаги')

    # В режиме fixed_with_topup при продлении сбрасываем трафик до фиксированного лимита
    # Только если не передан traffic_limit_gb И у подписки нет тарифа (классический режим)
    # Если у подписки есть tariff_id - трафик определяется тарифом, не сбрасываем
    if traffic_limit_gb is None and settings.is_traffic_fixed() and days > 0 and subscription.tariff_id is None:
        fixed_limit = settings.get_fixed_traffic_limit()
        old_limit = subscription.traffic_limit_gb
        if subscription.traffic_limit_gb != fixed_limit or (subscription.purchased_traffic_gb or 0) > 0:
            subscription.traffic_limit_gb = fixed_limit
            await db.execute(delete(TrafficPurchase).where(TrafficPurchase.subscription_id == subscription.id))
            subscription.purchased_traffic_gb = 0
            subscription.traffic_reset_at = None  # Сбрасываем дату сброса трафика
            logger.info(
                '🔄 Сброс трафика при продлении (fixed_with_topup): ГБ → ГБ',
                old_limit=old_limit,
                fixed_limit=fixed_limit,
            )

    subscription.updated_at = current_time

    if commit:
        await db.commit()
        await db.refresh(subscription, ['tariff'])
    else:
        await db.flush()

    await clear_notifications(db, subscription.id, commit=commit)

    logger.info('✅ Подписка продлена до', end_date=subscription.end_date)
    logger.info('📊 Новые параметры: статус=, окончание', status=subscription.status, end_date=subscription.end_date)

    return subscription


async def add_subscription_traffic(db: AsyncSession, subscription: Subscription, gb: int) -> Subscription:
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

    await db.commit()
    await db.refresh(subscription)

    logger.info(
        '📈 К подписке пользователя добавлено ГБ трафика (истекает )',
        user_id=subscription.user_id,
        gb=gb,
        new_expires_at=new_expires_at.strftime('%d.%m.%Y'),
    )
    return subscription


async def add_subscription_devices(db: AsyncSession, subscription: Subscription, devices: int) -> Subscription:
    # Lock subscription to prevent concurrent modifications
    locked_result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = locked_result.scalar_one()

    # Check max device limit
    max_devices = settings.MAX_DEVICES_LIMIT
    new_limit = (subscription.device_limit or 1) + devices
    if max_devices > 0 and new_limit > max_devices:
        logger.warning(
            '📱 Попытка превысить лимит устройств',
            user_id=subscription.user_id,
            current=subscription.device_limit,
            requested=devices,
            max_devices=max_devices,
        )
        new_limit = max_devices

    subscription.device_limit = new_limit
    subscription.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(subscription)

    logger.info('📱 К подписке пользователя добавлено устройств', user_id=subscription.user_id, devices=devices)
    return subscription


async def add_subscription_squad(db: AsyncSession, subscription: Subscription, squad_uuid: str) -> Subscription:
    if squad_uuid not in subscription.connected_squads:
        subscription.connected_squads = subscription.connected_squads + [squad_uuid]
        subscription.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(subscription)

        logger.info('🌍 К подписке пользователя добавлен сквад', user_id=subscription.user_id, squad_uuid=squad_uuid)

    return subscription


async def remove_subscription_squad(db: AsyncSession, subscription: Subscription, squad_uuid: str) -> Subscription:
    if squad_uuid in subscription.connected_squads:
        squads = subscription.connected_squads.copy()
        squads.remove(squad_uuid)
        subscription.connected_squads = squads
        subscription.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(subscription)

        logger.info('🚫 Из подписки пользователя удален сквад', user_id=subscription.user_id, squad_uuid=squad_uuid)

    return subscription


async def decrement_subscription_server_counts(
    db: AsyncSession,
    subscription: Subscription | None,
    *,
    subscription_servers: Iterable[SubscriptionServer] | None = None,
) -> None:
    """Decrease server counters linked to the provided subscription."""

    if not subscription:
        return

    # Save ID before any DB operations that might invalidate the ORM object
    sub_id = subscription.id

    server_ids: set[int] = set()

    if subscription_servers is not None:
        for sub_server in subscription_servers:
            if sub_server and sub_server.server_squad_id is not None:
                server_ids.add(sub_server.server_squad_id)
    else:
        try:
            ids_from_links = await get_subscription_server_ids(db, sub_id)
            server_ids.update(ids_from_links)
        except Exception as error:
            logger.error('⚠️ Не удалось получить серверы подписки для уменьшения счетчика', sub_id=sub_id, error=error)

    connected_squads = list(subscription.connected_squads or [])
    if connected_squads:
        try:
            from app.database.crud.server_squad import get_server_ids_by_uuids

            squad_server_ids = await get_server_ids_by_uuids(db, connected_squads)
            server_ids.update(squad_server_ids)
        except Exception as error:
            logger.error('⚠️ Не удалось сопоставить сквады подписки с серверами', sub_id=sub_id, error=error)

    if not server_ids:
        return

    try:
        from app.database.crud.server_squad import remove_user_from_servers

        # Use savepoint so StaleDataError rollback doesn't affect the parent transaction
        async with db.begin_nested():
            await remove_user_from_servers(db, list(server_ids))
    except StaleDataError:
        logger.warning(
            '⚠️ Подписка уже удалена (StaleDataError), пропускаем декремент серверов',
            sub_id=sub_id,
            list=list(server_ids),
        )
    except Exception as error:
        logger.error(
            '⚠️ Ошибка уменьшения счетчика пользователей серверов для подписки',
            list=list(server_ids),
            sub_id=sub_id,
            error=error,
        )


async def update_subscription_autopay(
    db: AsyncSession, subscription: Subscription, enabled: bool, days_before: int = 3
) -> Subscription:
    subscription.autopay_enabled = enabled
    subscription.autopay_days_before = days_before
    subscription.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(subscription)

    status = 'включен' if enabled else 'выключен'
    logger.info('💳 Автоплатеж для подписки пользователя', user_id=subscription.user_id, status=status)
    return subscription


async def deactivate_subscription(db: AsyncSession, subscription: Subscription) -> Subscription:
    subscription.status = SubscriptionStatus.DISABLED.value
    subscription.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(subscription)

    logger.info('❌ Подписка пользователя деактивирована', user_id=subscription.user_id)
    return subscription


async def reactivate_subscription(db: AsyncSession, subscription: Subscription) -> Subscription:
    """Реактивация подписки (например, после повторной подписки на канал или докупки трафика).

    Активирует если подписка была DISABLED или EXPIRED и ещё не истекла по времени.
    Не логирует если реактивация не требуется.
    """
    now = datetime.now(UTC)

    # Тихо выходим если реактивация не нужна (уже активна или другой статус)
    reactivatable_statuses = {
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.LIMITED.value,
    }
    if subscription.status not in reactivatable_statuses:
        return subscription

    if not subscription.end_date or subscription.end_date <= now:
        return subscription

    old_status = subscription.status
    subscription.status = SubscriptionStatus.ACTIVE.value
    subscription.updated_at = now

    await db.commit()
    await db.refresh(subscription)

    logger.info(
        '✅ Подписка реактивирована',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        old_status=old_status,
    )

    return subscription


async def get_expiring_subscriptions(db: AsyncSession, days_before: int = 3) -> list[Subscription]:
    from app.database.models import Tariff

    threshold_date = datetime.now(UTC) + timedelta(days=days_before)

    result = await db.execute(
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .outerjoin(Tariff, Subscription.tariff_id == Tariff.id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            and_(
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                User.status == UserStatus.ACTIVE.value,
                Subscription.end_date <= threshold_date,
                Subscription.end_date > datetime.now(UTC),
                # Не включаем активные суточные подписки — у них end_date всегда +24ч
                ~and_(
                    Tariff.is_daily.is_(True),
                    Subscription.is_daily_paused.is_(False),
                ),
            )
        )
    )
    return result.scalars().all()


async def get_expired_subscriptions(db: AsyncSession) -> list[Subscription]:
    from app.database.models import Tariff

    result = await db.execute(
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .outerjoin(Tariff, Subscription.tariff_id == Tariff.id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            and_(
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                User.status == UserStatus.ACTIVE.value,
                Subscription.end_date <= datetime.now(UTC),
                # Не трогаем активные суточные подписки — ими управляет DailySubscriptionService
                ~and_(
                    Tariff.is_daily.is_(True),
                    Subscription.is_daily_paused.is_(False),
                ),
            )
        )
    )
    return result.scalars().all()


async def get_subscriptions_for_autopay(db: AsyncSession) -> list[Subscription]:
    current_time = datetime.now(UTC)

    result = await db.execute(
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )
        .where(
            and_(
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                User.status == UserStatus.ACTIVE.value,
                Subscription.autopay_enabled == True,
                Subscription.is_trial == False,
            )
        )
    )
    all_autopay_subscriptions = result.scalars().all()

    ready_for_autopay = []
    for subscription in all_autopay_subscriptions:
        # Суточные подписки имеют свой механизм продления (DailySubscriptionService),
        # глобальный autopay на них не распространяется
        if subscription.tariff and getattr(subscription.tariff, 'is_daily', False):
            continue

        days_until_expiry = (subscription.end_date - current_time).days

        if days_until_expiry <= subscription.autopay_days_before and subscription.end_date > current_time:
            ready_for_autopay.append(subscription)

    return ready_for_autopay


async def get_subscriptions_statistics(db: AsyncSession) -> dict:
    total_result = await db.execute(select(func.count(Subscription.id)))
    total_subscriptions = total_result.scalar()

    active_result = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.ACTIVE.value)
    )
    active_subscriptions = active_result.scalar()

    trial_result = await db.execute(
        select(func.count(Subscription.id)).where(
            and_(Subscription.is_trial == True, Subscription.status == SubscriptionStatus.ACTIVE.value)
        )
    )
    trial_subscriptions = trial_result.scalar()

    paid_subscriptions = active_subscriptions - trial_subscriptions

    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today_start - timedelta(days=7)
    month_ago = today_start - timedelta(days=30)

    today_result = await db.execute(
        select(func.count(Transaction.id)).where(
            and_(
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed.is_(True),
                Transaction.created_at >= today_start,
            )
        )
    )
    purchased_today = today_result.scalar() or 0

    week_result = await db.execute(
        select(func.count(Transaction.id)).where(
            and_(
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed.is_(True),
                Transaction.created_at >= week_ago,
            )
        )
    )
    purchased_week = week_result.scalar() or 0

    month_result = await db.execute(
        select(func.count(Transaction.id)).where(
            and_(
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed.is_(True),
                Transaction.created_at >= month_ago,
            )
        )
    )
    purchased_month = month_result.scalar() or 0

    try:
        from app.database.crud.subscription_conversion import get_conversion_statistics

        conversion_stats = await get_conversion_statistics(db)

        trial_to_paid_conversion = conversion_stats.get('conversion_rate', 0)
        renewals_count = conversion_stats.get('month_conversions', 0)

        logger.info('📊 Статистика конверсии из таблицы conversions:')
        logger.info('Общее количество конверсий', get=conversion_stats.get('total_conversions', 0))
        logger.info('Процент конверсии', trial_to_paid_conversion=trial_to_paid_conversion)
        logger.info('Конверсий за месяц', renewals_count=renewals_count)

    except ImportError:
        logger.warning('⚠️ Таблица subscription_conversions не найдена, используем старую логику')

        users_with_paid_result = await db.execute(
            select(func.count(User.id)).where(User.has_had_paid_subscription == True)
        )
        users_with_paid = users_with_paid_result.scalar()

        total_users_result = await db.execute(select(func.count(User.id)))
        total_users = total_users_result.scalar()

        if total_users > 0:
            trial_to_paid_conversion = round((users_with_paid / total_users) * 100, 1)
        else:
            trial_to_paid_conversion = 0

        renewals_count = 0

    return {
        'total_subscriptions': total_subscriptions,
        'active_subscriptions': active_subscriptions,
        'trial_subscriptions': trial_subscriptions,
        'paid_subscriptions': paid_subscriptions,
        'purchased_today': purchased_today,
        'purchased_week': purchased_week,
        'purchased_month': purchased_month,
        'trial_to_paid_conversion': trial_to_paid_conversion,
        'renewals_count': renewals_count,
    }


async def get_trial_statistics(db: AsyncSession) -> dict:
    now = datetime.now(UTC)

    total_trials_result = await db.execute(select(func.count(Subscription.id)).where(Subscription.is_trial.is_(True)))
    total_trials = total_trials_result.scalar() or 0

    active_trials_result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.is_trial.is_(True),
            Subscription.end_date > now,
            Subscription.status.in_([SubscriptionStatus.TRIAL.value, SubscriptionStatus.ACTIVE.value]),
        )
    )
    active_trials = active_trials_result.scalar() or 0

    resettable_trials_result = await db.execute(
        select(func.count(Subscription.id))
        .join(User, Subscription.user_id == User.id)
        .where(
            Subscription.is_trial.is_(True),
            Subscription.end_date <= now,
            User.has_had_paid_subscription.is_(False),
        )
    )
    resettable_trials = resettable_trials_result.scalar() or 0

    return {
        'used_trials': total_trials,
        'active_trials': active_trials,
        'resettable_trials': resettable_trials,
    }


async def reset_trials_for_users_without_paid_subscription(db: AsyncSession) -> int:
    now = datetime.now(UTC)

    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.subscription_servers),
        )
        .join(User, Subscription.user_id == User.id)
        .where(
            Subscription.is_trial.is_(True),
            Subscription.end_date <= now,
            User.has_had_paid_subscription.is_(False),
        )
    )

    subscriptions = result.scalars().unique().all()
    if not subscriptions:
        return 0

    reset_count = len(subscriptions)
    for subscription in subscriptions:
        try:
            await decrement_subscription_server_counts(
                db,
                subscription,
                subscription_servers=subscription.subscription_servers,
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                'Не удалось обновить счётчики серверов при сбросе триала', subscription_id=subscription.id, error=error
            )

    subscription_ids = [subscription.id for subscription in subscriptions]

    if subscription_ids:
        try:
            await db.execute(delete(SubscriptionServer).where(SubscriptionServer.subscription_id.in_(subscription_ids)))
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error('Ошибка удаления серверных связей триалов', subscription_ids=subscription_ids, error=error)
            raise

        await db.execute(delete(Subscription).where(Subscription.id.in_(subscription_ids)))

    try:
        await db.commit()
    except Exception as error:  # pragma: no cover - defensive logging
        await db.rollback()
        logger.error('Ошибка сохранения сброса триалов', error=error)
        raise

    logger.info('♻️ Сброшено триальных подписок', reset_count=reset_count)
    return reset_count


async def update_subscription_usage(db: AsyncSession, subscription: Subscription, used_gb: float) -> Subscription:
    subscription.traffic_used_gb = used_gb
    subscription.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(subscription)

    return subscription


async def get_all_subscriptions(db: AsyncSession, page: int = 1, limit: int = 10) -> tuple[list[Subscription], int]:
    count_result = await db.execute(select(func.count(Subscription.id)))
    total_count = count_result.scalar()

    offset = (page - 1) * limit

    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .order_by(Subscription.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    subscriptions = result.scalars().all()

    return subscriptions, total_count


async def get_subscriptions_batch(
    db: AsyncSession,
    offset: int = 0,
    limit: int = 500,
) -> list[Subscription]:
    """Получает подписки пачками для синхронизации. Загружает связанных пользователей и тарифы."""
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .order_by(Subscription.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def add_subscription_servers(
    db: AsyncSession, subscription: Subscription, server_squad_ids: list[int], paid_prices: list[int] = None
) -> Subscription:
    await db.refresh(subscription)

    if paid_prices is None:
        now = datetime.now(UTC)
        days_remaining = max(1, (subscription.end_date - now).days)
        paid_prices = []

        from app.database.models import ServerSquad

        for server_id in server_squad_ids:
            result = await db.execute(select(ServerSquad.price_kopeks).where(ServerSquad.id == server_id))
            server_price_per_month = result.scalar() or 0
            total_price_for_period = int(server_price_per_month * days_remaining / 30)
            paid_prices.append(total_price_for_period)

    for i, server_id in enumerate(server_squad_ids):
        subscription_server = SubscriptionServer(
            subscription_id=subscription.id,
            server_squad_id=server_id,
            paid_price_kopeks=paid_prices[i] if i < len(paid_prices) else 0,
        )
        db.add(subscription_server)

    await db.commit()
    await db.refresh(subscription)

    logger.info(
        '🌐 К подписке добавлено серверов с ценами',
        subscription_id=subscription.id,
        server_squad_ids_count=len(server_squad_ids),
        paid_prices=paid_prices,
    )
    return subscription


async def get_subscription_server_ids(db: AsyncSession, subscription_id: int) -> list[int]:
    result = await db.execute(
        select(SubscriptionServer.server_squad_id).where(SubscriptionServer.subscription_id == subscription_id)
    )
    return [row[0] for row in result.fetchall()]


async def remove_subscription_servers(db: AsyncSession, subscription_id: int, server_squad_ids: list[int]) -> bool:
    try:
        from sqlalchemy import delete

        from app.database.models import SubscriptionServer

        await db.execute(
            delete(SubscriptionServer).where(
                SubscriptionServer.subscription_id == subscription_id,
                SubscriptionServer.server_squad_id.in_(server_squad_ids),
            )
        )

        await db.commit()
        logger.info('🗑️ Удалены серверы из подписки', server_squad_ids=server_squad_ids, subscription_id=subscription_id)
        return True

    except Exception as e:
        logger.error('Ошибка удаления серверов из подписки', error=e)
        await db.rollback()
        return False


async def expire_subscription(db: AsyncSession, subscription: Subscription) -> Subscription:
    subscription.status = SubscriptionStatus.EXPIRED.value
    subscription.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(subscription)

    logger.info('⏰ Подписка пользователя помечена как истёкшая', user_id=subscription.user_id)
    return subscription


async def check_and_update_subscription_status(db: AsyncSession, subscription: Subscription) -> Subscription:
    current_time = datetime.now(UTC)

    logger.info(
        '🔍 Проверка статуса подписки , текущий статус дата окончания текущее время',
        subscription_id=subscription.id,
        subscription_status=subscription.status,
        format_local_datetime=format_local_datetime(subscription.end_date),
        format_local_datetime_2=format_local_datetime(current_time),
    )

    # Для суточных тарифов с паузой не меняем статус на expired
    # (время "заморожено" пока пользователь на паузе)
    is_daily_paused = getattr(subscription, 'is_daily_paused', False)
    if is_daily_paused:
        logger.info('⏸️ Суточная подписка на паузе, пропускаем проверку истечения', subscription_id=subscription.id)
        return subscription

    # Активные суточные подписки управляются DailySubscriptionService — не экспайрим их тут.
    # end_date у них всего +24ч, и между проверками (30 мин) она может формально истечь.
    # Используем getattr(subscription, 'tariff', None) вместо property is_daily_tariff,
    # т.к. property может вызвать MissingGreenlet при ленивой загрузке в async-контексте.
    tariff = getattr(subscription, 'tariff', None)
    is_active_daily = tariff is not None and getattr(tariff, 'is_daily', False) and not is_daily_paused
    if is_active_daily:
        logger.debug(
            '⏩ Активная суточная подписка — пропускаем проверку истечения (управляет DailySubscriptionService)',
            subscription_id=subscription.id,
        )
        return subscription

    if subscription.status == SubscriptionStatus.ACTIVE.value and subscription.end_date <= current_time:
        # Детальное логирование для отладки проблемы с деактивацией
        time_diff = current_time - subscription.end_date
        logger.warning(
            '⏰ DEACTIVATION: подписка (user_id=) деактивируется в check_and_update_subscription_status. end_date=, current_time=, просрочена на',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
            end_date=subscription.end_date,
            current_time=current_time,
            time_diff=time_diff,
        )

        subscription.status = SubscriptionStatus.EXPIRED.value
        subscription.updated_at = current_time

        await db.commit()
        await db.refresh(subscription)

        logger.info("⏰ Статус подписки пользователя изменен на 'expired'", user_id=subscription.user_id)
    elif subscription.status == SubscriptionStatus.PENDING.value:
        logger.info('ℹ️ Проверка PENDING подписки статус остается без изменений', subscription_id=subscription.id)

    return subscription


async def create_subscription_no_commit(
    db: AsyncSession,
    user_id: int,
    status: str = 'trial',
    is_trial: bool = True,
    end_date: datetime = None,
    traffic_limit_gb: int = 10,
    traffic_used_gb: float = 0.0,
    device_limit: int = 1,
    connected_squads: list = None,
    remnawave_short_uuid: str = None,
    subscription_url: str = '',
    subscription_crypto_link: str = '',
    autopay_enabled: bool | None = None,
    autopay_days_before: int | None = None,
) -> Subscription:
    """
    Создает подписку без немедленного коммита для пакетной обработки
    """

    if end_date is None:
        end_date = datetime.now(UTC) + timedelta(days=3)

    if connected_squads is None:
        connected_squads = []

    subscription = Subscription(
        user_id=user_id,
        status=status,
        is_trial=is_trial,
        end_date=end_date,
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=traffic_used_gb,
        device_limit=device_limit,
        connected_squads=connected_squads,
        remnawave_short_uuid=remnawave_short_uuid,
        subscription_url=subscription_url,
        subscription_crypto_link=subscription_crypto_link,
        autopay_enabled=(settings.is_autopay_enabled_by_default() if autopay_enabled is None else autopay_enabled),
        autopay_days_before=(
            settings.DEFAULT_AUTOPAY_DAYS_BEFORE if autopay_days_before is None else autopay_days_before
        ),
    )

    db.add(subscription)

    # Выполняем flush, чтобы получить присвоенный первичный ключ
    await db.flush()

    # Не коммитим сразу, оставляем для пакетной обработки
    logger.info('✅ Подготовлена подписка для пользователя (ожидает коммита)', user_id=user_id)
    return subscription


async def create_subscription(
    db: AsyncSession,
    user_id: int,
    status: str = 'trial',
    is_trial: bool = True,
    end_date: datetime = None,
    traffic_limit_gb: int = 10,
    traffic_used_gb: float = 0.0,
    device_limit: int = 1,
    connected_squads: list = None,
    remnawave_short_uuid: str = None,
    subscription_url: str = '',
    subscription_crypto_link: str = '',
    autopay_enabled: bool | None = None,
    autopay_days_before: int | None = None,
) -> Subscription:
    if end_date is None:
        end_date = datetime.now(UTC) + timedelta(days=3)

    if connected_squads is None:
        connected_squads = []

    subscription = Subscription(
        user_id=user_id,
        status=status,
        is_trial=is_trial,
        end_date=end_date,
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=traffic_used_gb,
        device_limit=device_limit,
        connected_squads=connected_squads,
        remnawave_short_uuid=remnawave_short_uuid,
        subscription_url=subscription_url,
        subscription_crypto_link=subscription_crypto_link,
        autopay_enabled=(settings.is_autopay_enabled_by_default() if autopay_enabled is None else autopay_enabled),
        autopay_days_before=(
            settings.DEFAULT_AUTOPAY_DAYS_BEFORE if autopay_days_before is None else autopay_days_before
        ),
    )

    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)

    logger.info('✅ Создана подписка для пользователя', user_id=user_id)
    return subscription


async def create_pending_subscription(
    db: AsyncSession,
    user_id: int,
    duration_days: int,
    traffic_limit_gb: int = 0,
    device_limit: int = 1,
    connected_squads: list[str] = None,
    payment_method: str = 'pending',
    total_price_kopeks: int = 0,
    is_trial: bool = False,
    tariff_id: int | None = None,
) -> Subscription:
    """Creates a pending subscription that will be activated after payment.

    Args:
        is_trial: If True, marks the subscription as a trial subscription.
    """
    trial_label = 'триальная ' if is_trial else ''
    current_time = datetime.now(UTC)
    end_date = current_time + timedelta(days=duration_days)

    existing_subscription = await get_subscription_by_user_id(db, user_id)

    if existing_subscription:
        if (
            existing_subscription.status == SubscriptionStatus.ACTIVE.value
            and existing_subscription.end_date > current_time
        ):
            logger.warning(
                '⚠️ Попытка создать pending подписку для активного пользователя . Возвращаем существующую запись.',
                trial_label=trial_label,
                user_id=user_id,
            )
            return existing_subscription

        existing_subscription.status = SubscriptionStatus.PENDING.value
        existing_subscription.is_trial = is_trial
        existing_subscription.start_date = current_time
        existing_subscription.end_date = end_date
        existing_subscription.traffic_limit_gb = traffic_limit_gb
        existing_subscription.device_limit = device_limit
        existing_subscription.connected_squads = connected_squads or []
        existing_subscription.traffic_used_gb = 0.0
        existing_subscription.updated_at = current_time
        if tariff_id is not None:
            existing_subscription.tariff_id = tariff_id

        await db.commit()
        await db.refresh(existing_subscription)

        logger.info(
            '♻️ Обновлена ожидающая подписка пользователя , ID метод оплаты',
            trial_label=trial_label,
            user_id=user_id,
            existing_subscription_id=existing_subscription.id,
            payment_method=payment_method,
        )
        return existing_subscription

    subscription = Subscription(
        user_id=user_id,
        status=SubscriptionStatus.PENDING.value,
        is_trial=is_trial,
        start_date=current_time,
        end_date=end_date,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        connected_squads=connected_squads or [],
        tariff_id=tariff_id,
        autopay_enabled=settings.is_autopay_enabled_by_default(),
        autopay_days_before=settings.DEFAULT_AUTOPAY_DAYS_BEFORE,
    )

    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)

    logger.info(
        '💳 Создана ожидающая подписка для пользователя , ID метод оплаты',
        trial_label=trial_label,
        user_id=user_id,
        subscription_id=subscription.id,
        payment_method=payment_method,
    )

    return subscription


# Обратная совместимость: алиас для триальной подписки
async def create_pending_trial_subscription(
    db: AsyncSession,
    user_id: int,
    duration_days: int,
    traffic_limit_gb: int = 0,
    device_limit: int = 1,
    connected_squads: list[str] = None,
    payment_method: str = 'pending',
    total_price_kopeks: int = 0,
    tariff_id: int | None = None,
) -> Subscription:
    """Creates a pending trial subscription. Wrapper for create_pending_subscription with is_trial=True."""
    return await create_pending_subscription(
        db=db,
        user_id=user_id,
        duration_days=duration_days,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        connected_squads=connected_squads,
        payment_method=payment_method,
        total_price_kopeks=total_price_kopeks,
        is_trial=True,
        tariff_id=tariff_id,
    )


async def activate_pending_subscription(db: AsyncSession, user_id: int, period_days: int = None) -> Subscription | None:
    """Активирует pending подписку пользователя, меняя её статус на ACTIVE."""
    logger.info('Активация pending подписки: пользователь период дней', user_id=user_id, period_days=period_days)

    # Находим pending подписку пользователя
    result = await db.execute(
        select(Subscription).where(
            and_(Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.PENDING.value)
        )
    )
    pending_subscription = result.scalar_one_or_none()

    if not pending_subscription:
        logger.warning('Не найдена pending подписка для пользователя', user_id=user_id)
        return None

    logger.info(
        'Найдена pending подписка для пользователя статус',
        pending_subscription_id=pending_subscription.id,
        user_id=user_id,
        status=pending_subscription.status,
    )

    # Обновляем статус подписки на ACTIVE
    current_time = datetime.now(UTC)
    pending_subscription.status = SubscriptionStatus.ACTIVE.value

    # Если указан период, обновляем дату окончания
    if period_days is not None:
        effective_start = pending_subscription.start_date or current_time
        effective_start = max(effective_start, current_time)
        pending_subscription.end_date = effective_start + timedelta(days=period_days)

    # Обновляем дату начала, если она не установлена или в прошлом
    if not pending_subscription.start_date or pending_subscription.start_date < current_time:
        pending_subscription.start_date = current_time

    await db.commit()
    await db.refresh(pending_subscription)

    logger.info(
        'Подписка пользователя активирована, ID', user_id=user_id, pending_subscription_id=pending_subscription.id
    )

    return pending_subscription


async def activate_pending_trial_subscription(
    db: AsyncSession,
    subscription_id: int,
    user_id: int,
) -> Subscription | None:
    """Активирует pending триальную подписку по её ID после оплаты."""
    logger.info(
        'Активация pending триальной подписки: subscription_id=, user_id',
        subscription_id=subscription_id,
        user_id=user_id,
    )

    # Находим pending подписку по ID
    result = await db.execute(
        select(Subscription).where(
            and_(
                Subscription.id == subscription_id,
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.PENDING.value,
                Subscription.is_trial == True,
            )
        )
    )
    pending_subscription = result.scalar_one_or_none()

    if not pending_subscription:
        logger.warning(
            'Не найдена pending триальная подписка для пользователя', subscription_id=subscription_id, user_id=user_id
        )
        return None

    logger.info(
        'Найдена pending триальная подписка статус',
        pending_subscription_id=pending_subscription.id,
        status=pending_subscription.status,
    )

    # Обновляем статус подписки на ACTIVE
    current_time = datetime.now(UTC)
    pending_subscription.status = SubscriptionStatus.ACTIVE.value

    # Обновляем даты
    if not pending_subscription.start_date or pending_subscription.start_date < current_time:
        pending_subscription.start_date = current_time

    # Пересчитываем end_date на основе duration_days если есть
    duration_days = pending_subscription.duration_days if hasattr(pending_subscription, 'duration_days') else None
    if duration_days:
        pending_subscription.end_date = current_time + timedelta(days=duration_days)
    elif pending_subscription.end_date and pending_subscription.end_date < current_time:
        # Если end_date в прошлом, пересчитываем
        from app.config import settings

        pending_subscription.end_date = current_time + timedelta(days=settings.TRIAL_DURATION_DAYS)

    await db.commit()
    await db.refresh(pending_subscription)

    logger.info(
        'Триальная подписка активирована для пользователя',
        pending_subscription_id=pending_subscription.id,
        user_id=user_id,
    )

    return pending_subscription


# ==================== СУТОЧНЫЕ ПОДПИСКИ ====================


async def get_daily_subscriptions_for_charge(db: AsyncSession) -> list[Subscription]:
    """
    Получает все суточные подписки, которые нужно обработать для списания.

    Критерии:
    - Тариф подписки суточный (is_daily=True)
    - Подписка активна
    - Подписка не приостановлена пользователем
    - Прошло более 24 часов с последнего списания (или списания ещё не было)
    """
    from app.database.models import Tariff

    now = datetime.now(UTC)
    one_day_ago = now - timedelta(hours=24)

    query = (
        select(Subscription)
        .join(Tariff, Subscription.tariff_id == Tariff.id)
        .join(User, Subscription.user_id == User.id)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )
        .where(
            and_(
                Tariff.is_daily.is_(True),
                Tariff.is_active.is_(True),
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                User.status == UserStatus.ACTIVE.value,
                Subscription.is_daily_paused.is_(False),
                Subscription.is_trial.is_(False),  # Не списываем с триальных подписок
                # Списания ещё не было ИЛИ прошло более 24 часов
                ((Subscription.last_daily_charge_at.is_(None)) | (Subscription.last_daily_charge_at < one_day_ago)),
            )
        )
    )

    result = await db.execute(query)
    subscriptions = result.scalars().all()

    logger.info('🔍 Найдено суточных подписок для списания', subscriptions_count=len(subscriptions))

    return list(subscriptions)


async def get_disabled_daily_subscriptions_for_resume(
    db: AsyncSession,
) -> list[Subscription]:
    """
    Получает список DISABLED суточных подписок, которые можно возобновить.
    Подписки с достаточным балансом пользователя будут возобновлены.
    """
    from app.database.models import Tariff, User

    query = (
        select(Subscription)
        .join(Tariff, Subscription.tariff_id == Tariff.id)
        .join(User, Subscription.user_id == User.id)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )
        .where(
            and_(
                Tariff.is_daily.is_(True),
                Tariff.is_active.is_(True),
                Subscription.status == SubscriptionStatus.DISABLED.value,
                User.status == UserStatus.ACTIVE.value,
                Subscription.is_trial.is_(False),
                # Не возобновляем подписки, приостановленные пользователем вручную
                # is_(False) не ловит NULL, поэтому добавляем OR is_(None)
                (Subscription.is_daily_paused.is_(False) | Subscription.is_daily_paused.is_(None)),
                # Баланс пользователя > 0 (permissive pre-filter;
                # actual discounted price check happens in _process_single_charge)
                User.balance_kopeks > 0,
            )
        )
    )

    result = await db.execute(query)
    subscriptions = result.scalars().all()

    logger.info('🔍 Найдено DISABLED суточных подписок для возобновления', subscriptions_count=len(subscriptions))

    return list(subscriptions)


async def get_expired_daily_subscriptions_for_recovery(db: AsyncSession) -> list[Subscription]:
    """
    Получает EXPIRED суточные подписки, которые были ошибочно экспайрены
    middleware или check_and_update_subscription_status.

    Суточные подписки не должны экспайриться — ими управляет DailySubscriptionService.
    Если баланс пользователя достаточен, подписку нужно восстановить и списать.
    """
    from app.database.models import Tariff

    # Берём только недавно экспайренные (до 24ч) — старые не трогаем
    recovery_threshold = datetime.now(UTC) - timedelta(hours=24)

    query = (
        select(Subscription)
        .join(Tariff, Subscription.tariff_id == Tariff.id)
        .join(User, Subscription.user_id == User.id)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )
        .where(
            and_(
                Tariff.is_daily.is_(True),
                Tariff.is_active.is_(True),
                Subscription.status == SubscriptionStatus.EXPIRED.value,
                User.status == UserStatus.ACTIVE.value,
                # is_(False) не ловит NULL, поэтому добавляем OR is_(None)
                (Subscription.is_daily_paused.is_(False) | Subscription.is_daily_paused.is_(None)),
                Subscription.is_trial.is_(False),
                # Только недавно экспайренные
                Subscription.updated_at >= recovery_threshold,
                # Баланс > 0 (permissive pre-filter;
                # actual discounted price check happens in _process_single_charge)
                User.balance_kopeks > 0,
            )
        )
    )

    result = await db.execute(query)
    subscriptions = result.scalars().all()

    if subscriptions:
        logger.warning(
            '⚠️ Найдено EXPIRED суточных подписок для восстановления (ошибочно экспайрены)',
            subscriptions_count=len(subscriptions),
        )

    return list(subscriptions)


async def pause_daily_subscription(
    db: AsyncSession,
    subscription: Subscription,
) -> Subscription:
    """Приостанавливает суточную подписку (списание не будет происходить)."""
    if not subscription.is_daily_tariff:
        logger.warning('Попытка приостановить не-суточную подписку', subscription_id=subscription.id)
        return subscription

    subscription.is_daily_paused = True
    await db.commit()
    await db.refresh(subscription)

    logger.info(
        '⏸️ Суточная подписка приостановлена пользователем',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
    )

    return subscription


async def resume_daily_subscription(
    db: AsyncSession,
    subscription: Subscription,
) -> Subscription:
    """Возобновляет суточную подписку (списание продолжится)."""
    if not subscription.is_daily_tariff:
        logger.warning('Попытка возобновить не-суточную подписку', subscription_id=subscription.id)
        return subscription

    subscription.is_daily_paused = False

    # Восстанавливаем статус ACTIVE если подписка была DISABLED/EXPIRED/LIMITED
    if subscription.status in (
        SubscriptionStatus.DISABLED.value,
        SubscriptionStatus.EXPIRED.value,
        SubscriptionStatus.LIMITED.value,
    ):
        previous_status = subscription.status
        subscription.status = SubscriptionStatus.ACTIVE.value
        # Обновляем время последнего списания для корректного расчёта следующего
        subscription.last_daily_charge_at = datetime.now(UTC)
        subscription.end_date = datetime.now(UTC) + timedelta(days=1)
        logger.info(
            '✅ Суточная подписка восстановлена из в ACTIVE',
            subscription_id=subscription.id,
            previous_status=previous_status,
        )

    await db.commit()
    await db.refresh(subscription)

    logger.info(
        '▶️ Суточная подписка возобновлена пользователем', subscription_id=subscription.id, user_id=subscription.user_id
    )

    return subscription


async def update_daily_charge_time(
    db: AsyncSession,
    subscription: Subscription,
    charge_time: datetime = None,
) -> Subscription:
    """Обновляет время последнего суточного списания и продлевает подписку на 1 день."""
    now = charge_time or datetime.now(UTC)
    subscription.last_daily_charge_at = now

    # Продлеваем подписку на 1 день от текущего момента
    new_end_date = now + timedelta(days=1)
    if subscription.end_date is None or subscription.end_date < new_end_date:
        subscription.end_date = new_end_date
        logger.info('📅 Продлена подписка до', subscription_id=subscription.id, new_end_date=new_end_date)

    await db.commit()
    await db.refresh(subscription)

    return subscription


async def suspend_daily_subscription_insufficient_balance(
    db: AsyncSession,
    subscription: Subscription,
) -> Subscription:
    """
    Приостанавливает подписку из-за недостатка баланса.
    Отличается от pause_daily_subscription тем, что меняет статус на DISABLED.
    """
    subscription.status = SubscriptionStatus.DISABLED.value
    await db.commit()
    await db.refresh(subscription)

    logger.info(
        '⚠️ Суточная подписка приостановлена: недостаточно средств (user_id=)',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
    )

    return subscription


async def get_subscription_with_tariff(
    db: AsyncSession,
    user_id: int,
) -> Subscription | None:
    """Получает подписку пользователя с загруженным тарифом."""
    result = await db.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.tariff),
        )
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()

    if subscription:
        subscription = await check_and_update_subscription_status(db, subscription)

    return subscription


async def toggle_daily_subscription_pause(
    db: AsyncSession,
    subscription: Subscription,
) -> Subscription:
    """Переключает состояние паузы суточной подписки."""
    if subscription.is_daily_paused:
        return await resume_daily_subscription(db, subscription)
    return await pause_daily_subscription(db, subscription)
