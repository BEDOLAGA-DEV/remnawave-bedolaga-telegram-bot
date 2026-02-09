import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import PromoGroup, Subscription, Tariff


logger = logging.getLogger(__name__)


def _normalize_period_prices(period_prices: dict[int, int] | None) -> dict[str, int]:
    """Нормализует цены периодов в формат {str: int}."""
    if not period_prices:
        return {}

    normalized: dict[str, int] = {}

    for key, value in period_prices.items():
        try:
            period = int(key)
            price = int(value)
        except (TypeError, ValueError):
            continue

        if period > 0 and price >= 0:
            normalized[str(period)] = price

    return normalized


async def get_all_tariffs(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> list[Tariff]:
    """Получает все тарифы с опциональной фильтрацией по активности."""
    query = select(Tariff).options(selectinload(Tariff.allowed_promo_groups))

    if not include_inactive:
        query = query.where(Tariff.is_active.is_(True))

    query = query.order_by(Tariff.display_order, Tariff.id)

    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


async def get_tariff_by_id(
    db: AsyncSession,
    tariff_id: int,
    *,
    with_promo_groups: bool = True,
) -> Tariff | None:
    """Получает тариф по ID."""
    query = select(Tariff).where(Tariff.id == tariff_id)

    if with_promo_groups:
        query = query.options(selectinload(Tariff.allowed_promo_groups))

    result = await db.execute(query)
    return result.scalars().first()


async def count_tariffs(db: AsyncSession, *, include_inactive: bool = False) -> int:
    """Подсчитывает количество тарифов."""
    query = select(func.count(Tariff.id))

    if not include_inactive:
        query = query.where(Tariff.is_active.is_(True))

    result = await db.execute(query)
    return int(result.scalar_one())


async def get_trial_tariff(db: AsyncSession) -> Tariff | None:
    """Получает тариф, доступный для триала (is_trial_available=True).

    Сортируется по updated_at DESC, чтобы вернуть последний установленный
    триальный тариф (на случай если их несколько).
    """
    query = (
        select(Tariff)
        .where(Tariff.is_trial_available.is_(True))
        .where(Tariff.is_active.is_(True))
        .options(selectinload(Tariff.allowed_promo_groups))
        .order_by(Tariff.updated_at.desc().nullslast(), Tariff.id.desc())
        .limit(1)
    )
    result = await db.execute(query)
    return result.scalars().first()


async def set_trial_tariff(db: AsyncSession, tariff_id: int) -> Tariff | None:
    """Устанавливает тариф как триальный (снимает флаг с других тарифов)."""
    # Снимаем флаг с всех тарифов
    await db.execute(Tariff.__table__.update().values(is_trial_available=False))

    # Устанавливаем флаг на выбранный тариф
    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff:
        tariff.is_trial_available = True
        await db.commit()
        await db.refresh(tariff)

    return tariff


async def clear_trial_tariff(db: AsyncSession) -> None:
    """Снимает флаг триала со всех тарифов."""
    await db.execute(Tariff.__table__.update().values(is_trial_available=False))
    await db.commit()


async def get_tariffs_for_user(
    db: AsyncSession,
    promo_group_id: int | None = None,
) -> list[Tariff]:
    """
    Получает тарифы, доступные для пользователя с учетом его промогруппы.
    Если у тарифа нет ограничений по промогруппам - он доступен всем.
    """
    query = (
        select(Tariff)
        .options(selectinload(Tariff.allowed_promo_groups))
        .where(Tariff.is_active.is_(True))
        .order_by(Tariff.display_order, Tariff.id)
    )

    result = await db.execute(query)
    tariffs = result.scalars().all()

    # Фильтруем по промогруппе
    available_tariffs = []
    for tariff in tariffs:
        if not tariff.allowed_promo_groups:
            # Нет ограничений - доступен всем
            available_tariffs.append(tariff)
        elif promo_group_id is not None:
            # Проверяем, есть ли промогруппа пользователя в списке разрешенных
            if any(pg.id == promo_group_id for pg in tariff.allowed_promo_groups):
                available_tariffs.append(tariff)
        # else: пользователь без промогруппы, а у тарифа есть ограничения - пропускаем

    return available_tariffs


async def create_tariff(
    db: AsyncSession,
    name: str,
    *,
    description: str | None = None,
    display_order: int = 0,
    is_active: bool = True,
    traffic_limit_gb: int = 100,
    device_limit: int = 1,
    device_price_kopeks: int | None = None,
    max_device_limit: int | None = None,
    allowed_squads: list[str] | None = None,
    server_traffic_limits: dict[str, dict] | None = None,
    period_prices: dict[int, int] | None = None,
    tier_level: int = 1,
    is_trial_available: bool = False,
    allow_traffic_topup: bool = True,
    promo_group_ids: list[int] | None = None,
    traffic_topup_enabled: bool = False,
    traffic_topup_packages: dict[str, int] | None = None,
    max_topup_traffic_gb: int = 0,
    is_daily: bool = False,
    daily_price_kopeks: int = 0,
    # Произвольное количество дней
    custom_days_enabled: bool = False,
    price_per_day_kopeks: int = 0,
    min_days: int = 1,
    max_days: int = 365,
    # Произвольный трафик при покупке
    custom_traffic_enabled: bool = False,
    traffic_price_per_gb_kopeks: int = 0,
    min_traffic_gb: int = 1,
    max_traffic_gb: int = 1000,
    # Режим сброса трафика
    traffic_reset_mode: str | None = None,  # DAY, WEEK, MONTH, NO_RESET, None = глобальная настройка
) -> Tariff:
    """Создает новый тариф."""
    normalized_prices = _normalize_period_prices(period_prices)

    tariff = Tariff(
        name=name.strip(),
        description=description.strip() if description else None,
        display_order=max(0, display_order),
        is_active=is_active,
        traffic_limit_gb=max(0, traffic_limit_gb),
        device_limit=max(1, device_limit),
        device_price_kopeks=device_price_kopeks,
        max_device_limit=max_device_limit,
        allowed_squads=allowed_squads or [],
        server_traffic_limits=server_traffic_limits or {},
        period_prices=normalized_prices,
        tier_level=max(1, tier_level),
        is_trial_available=is_trial_available,
        allow_traffic_topup=allow_traffic_topup,
        traffic_topup_enabled=traffic_topup_enabled,
        traffic_topup_packages=traffic_topup_packages or {},
        max_topup_traffic_gb=max(0, max_topup_traffic_gb),
        is_daily=is_daily,
        daily_price_kopeks=max(0, daily_price_kopeks),
        # Произвольное количество дней
        custom_days_enabled=custom_days_enabled,
        price_per_day_kopeks=max(0, price_per_day_kopeks),
        min_days=max(1, min_days),
        max_days=max(1, max_days),
        # Произвольный трафик при покупке
        custom_traffic_enabled=custom_traffic_enabled,
        traffic_price_per_gb_kopeks=max(0, traffic_price_per_gb_kopeks),
        min_traffic_gb=max(1, min_traffic_gb),
        max_traffic_gb=max(1, max_traffic_gb),
        # Режим сброса трафика
        traffic_reset_mode=traffic_reset_mode,
    )

    db.add(tariff)
    await db.flush()

    # Добавляем промогруппы если указаны
    if promo_group_ids:
        promo_groups_result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(promo_group_ids)))
        promo_groups = promo_groups_result.scalars().all()
        # Refresh чтобы избежать lazy load в async контексте
        await db.refresh(tariff, ['allowed_promo_groups'])
        tariff.allowed_promo_groups = list(promo_groups)

    await db.commit()
    await db.refresh(tariff)

    logger.info(
        "Создан тариф '%s' (id=%s, tier=%s, traffic=%sGB, devices=%s, prices=%s)",
        tariff.name,
        tariff.id,
        tariff.tier_level,
        tariff.traffic_limit_gb,
        tariff.device_limit,
        normalized_prices,
    )

    return tariff


async def update_tariff(
    db: AsyncSession,
    tariff: Tariff,
    *,
    name: str | None = None,
    description: str | None = None,
    display_order: int | None = None,
    is_active: bool | None = None,
    traffic_limit_gb: int | None = None,
    device_limit: int | None = None,
    device_price_kopeks: int | None = ...,  # ... = не передан, None = сбросить
    max_device_limit: int | None = ...,  # ... = не передан, None = сбросить (без лимита)
    allowed_squads: list[str] | None = None,
    server_traffic_limits: dict[str, dict] | None = None,
    period_prices: dict[int, int] | None = None,
    tier_level: int | None = None,
    is_trial_available: bool | None = None,
    allow_traffic_topup: bool | None = None,
    promo_group_ids: list[int] | None = None,
    traffic_topup_enabled: bool | None = None,
    traffic_topup_packages: dict[str, int] | None = None,
    max_topup_traffic_gb: int | None = None,
    is_daily: bool | None = None,
    daily_price_kopeks: int | None = None,
    # Произвольное количество дней
    custom_days_enabled: bool | None = None,
    price_per_day_kopeks: int | None = None,
    min_days: int | None = None,
    max_days: int | None = None,
    # Произвольный трафик при покупке
    custom_traffic_enabled: bool | None = None,
    traffic_price_per_gb_kopeks: int | None = None,
    min_traffic_gb: int | None = None,
    max_traffic_gb: int | None = None,
    # Режим сброса трафика
    traffic_reset_mode: str | None = ...,  # ... = не передан, None = сбросить к глобальной настройке
) -> Tariff:
    """Обновляет существующий тариф."""
    if name is not None:
        tariff.name = name.strip()
    if description is not None:
        tariff.description = description.strip() if description else None
    if display_order is not None:
        tariff.display_order = max(0, display_order)
    if is_active is not None:
        tariff.is_active = is_active
    if traffic_limit_gb is not None:
        tariff.traffic_limit_gb = max(0, traffic_limit_gb)
    if device_limit is not None:
        tariff.device_limit = max(1, device_limit)
    if device_price_kopeks is not ...:
        # Если передан device_price_kopeks (включая None) - обновляем
        tariff.device_price_kopeks = device_price_kopeks
    if max_device_limit is not ...:
        # Если передан max_device_limit (включая None) - обновляем
        tariff.max_device_limit = max_device_limit
    if allowed_squads is not None:
        tariff.allowed_squads = allowed_squads
    if server_traffic_limits is not None:
        tariff.server_traffic_limits = server_traffic_limits
    if allow_traffic_topup is not None:
        tariff.allow_traffic_topup = allow_traffic_topup
    if period_prices is not None:
        tariff.period_prices = _normalize_period_prices(period_prices)
    if tier_level is not None:
        tariff.tier_level = max(1, tier_level)
    if is_trial_available is not None:
        tariff.is_trial_available = is_trial_available
    if traffic_topup_enabled is not None:
        tariff.traffic_topup_enabled = traffic_topup_enabled
    if traffic_topup_packages is not None:
        tariff.traffic_topup_packages = traffic_topup_packages
    if max_topup_traffic_gb is not None:
        tariff.max_topup_traffic_gb = max(0, max_topup_traffic_gb)
    if is_daily is not None:
        tariff.is_daily = is_daily
    if daily_price_kopeks is not None:
        tariff.daily_price_kopeks = max(0, daily_price_kopeks)
    # Произвольное количество дней
    if custom_days_enabled is not None:
        tariff.custom_days_enabled = custom_days_enabled
    if price_per_day_kopeks is not None:
        tariff.price_per_day_kopeks = max(0, price_per_day_kopeks)
    if min_days is not None:
        tariff.min_days = max(1, min_days)
    if max_days is not None:
        tariff.max_days = max(1, max_days)
    # Произвольный трафик при покупке
    if custom_traffic_enabled is not None:
        tariff.custom_traffic_enabled = custom_traffic_enabled
    if traffic_price_per_gb_kopeks is not None:
        tariff.traffic_price_per_gb_kopeks = max(0, traffic_price_per_gb_kopeks)
    if min_traffic_gb is not None:
        tariff.min_traffic_gb = max(1, min_traffic_gb)
    if max_traffic_gb is not None:
        tariff.max_traffic_gb = max(1, max_traffic_gb)
    # Режим сброса трафика
    if traffic_reset_mode is not ...:
        tariff.traffic_reset_mode = traffic_reset_mode

    # Обновляем промогруппы если указаны
    if promo_group_ids is not None:
        if promo_group_ids:
            promo_groups_result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(promo_group_ids)))
            promo_groups = promo_groups_result.scalars().all()
            tariff.allowed_promo_groups = list(promo_groups)
        else:
            tariff.allowed_promo_groups = []

    await db.commit()
    await db.refresh(tariff)

    logger.info(
        "Обновлен тариф '%s' (id=%s)",
        tariff.name,
        tariff.id,
    )

    return tariff


async def delete_tariff(db: AsyncSession, tariff: Tariff) -> bool:
    """
    Удаляет тариф.
    Подписки с этим тарифом получат tariff_id = NULL.
    """
    tariff_id = tariff.id
    tariff_name = tariff.name

    # Подсчитываем подписки с этим тарифом
    subscriptions_count = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.tariff_id == tariff_id)
    )
    affected_subscriptions = subscriptions_count.scalar_one()

    # Удаляем тариф (FK с ondelete=SET NULL автоматически обнулит tariff_id в подписках)
    await db.delete(tariff)
    await db.commit()

    logger.info(
        "Удален тариф '%s' (id=%s), затронуто подписок: %s",
        tariff_name,
        tariff_id,
        affected_subscriptions,
    )

    return True


async def get_tariff_subscriptions_count(db: AsyncSession, tariff_id: int) -> int:
    """Подсчитывает количество подписок на тарифе."""
    result = await db.execute(select(func.count(Subscription.id)).where(Subscription.tariff_id == tariff_id))
    return int(result.scalar_one())


async def set_tariff_promo_groups(
    db: AsyncSession,
    tariff: Tariff,
    promo_group_ids: list[int],
) -> Tariff:
    """Устанавливает промогруппы для тарифа."""
    if promo_group_ids:
        promo_groups_result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(promo_group_ids)))
        promo_groups = promo_groups_result.scalars().all()
        tariff.allowed_promo_groups = list(promo_groups)
    else:
        tariff.allowed_promo_groups = []

    await db.commit()
    await db.refresh(tariff)

    return tariff


async def add_promo_group_to_tariff(
    db: AsyncSession,
    tariff: Tariff,
    promo_group_id: int,
) -> bool:
    """Добавляет промогруппу к тарифу."""
    promo_group = await db.get(PromoGroup, promo_group_id)
    if not promo_group:
        return False

    if promo_group not in tariff.allowed_promo_groups:
        tariff.allowed_promo_groups.append(promo_group)
        await db.commit()

    return True


async def remove_promo_group_from_tariff(
    db: AsyncSession,
    tariff: Tariff,
    promo_group_id: int,
) -> bool:
    """Удаляет промогруппу из тарифа."""
    for pg in tariff.allowed_promo_groups:
        if pg.id == promo_group_id:
            tariff.allowed_promo_groups.remove(pg)
            await db.commit()
            return True
    return False


async def get_tariffs_with_subscriptions_count(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
) -> list[tuple]:
    """Получает тарифы с количеством подписок."""
    query = (
        select(Tariff, func.count(Subscription.id))
        .outerjoin(Subscription, Subscription.tariff_id == Tariff.id)
        .group_by(Tariff.id)
        .order_by(Tariff.display_order, Tariff.id)
    )

    if not include_inactive:
        query = query.where(Tariff.is_active.is_(True))

    result = await db.execute(query)
    return result.all()


async def reorder_tariffs(
    db: AsyncSession,
    tariff_order: list[int],
) -> None:
    """Изменяет порядок отображения тарифов."""
    for order, tariff_id in enumerate(tariff_order):
        await db.execute(update(Tariff).where(Tariff.id == tariff_id).values(display_order=order))

    logger.info('Изменен порядок тарифов: %s', tariff_order)


async def sync_default_tariff_from_config(db: AsyncSession) -> Tariff | None:
    """
    Синхронизирует дефолтный тариф из конфига (.env) в БД.
    Создаёт тариф "Стандартный" только если в БД нет тарифов.
    Существующий тариф НЕ перезаписывается — админ управляет им через кабинет.

    Returns:
        Tariff или None если не требуется синхронизация
    """
    from app.config import PERIOD_PRICES, settings

    # Проверяем есть ли тарифы в БД
    result = await db.execute(select(func.count(Tariff.id)))
    tariff_count = result.scalar() or 0

    # Собираем цены из конфига
    period_prices = {}
    for period, price in PERIOD_PRICES.items():
        if price > 0:
            period_prices[str(period)] = price

    if not period_prices:
        logger.warning('Нет цен в конфиге для создания дефолтного тарифа')
        return None

    # Ищем тариф с именем "Стандартный" или первый тариф
    result = await db.execute(select(Tariff).where(Tariff.name == 'Стандартный').limit(1))
    existing_tariff = result.scalar_one_or_none()

    if existing_tariff:
        # Тариф уже существует — НЕ перезаписываем настройки из конфига.
        # Админ управляет тарифом через кабинет, синхронизация не нужна.
        logger.info(
            "Дефолтный тариф 'Стандартный' (id=%s) уже существует, пропускаем sync из конфига", existing_tariff.id
        )
        return existing_tariff

    if tariff_count == 0:
        # Создаём новый дефолтный тариф
        new_tariff = Tariff(
            name='Стандартный',
            description='Базовый тарифный план',
            is_active=True,
            is_trial_available=True,
            traffic_limit_gb=settings.DEFAULT_TRAFFIC_LIMIT_GB,
            device_limit=settings.DEFAULT_DEVICE_LIMIT,
            tier_level=1,
            display_order=0,
            period_prices=period_prices,
            allowed_squads=[],  # Все серверы по умолчанию
            server_traffic_limits={},
        )
        db.add(new_tariff)
        await db.commit()
        await db.refresh(new_tariff)
        logger.info("Создан дефолтный тариф 'Стандартный' из конфига: %s", period_prices)
        return new_tariff

    return None


async def load_period_prices_from_db(db: AsyncSession) -> None:
    """
    Загружает периоды/цены из тарифа в PERIOD_PRICES.
    Работает ТОЛЬКО в режиме tariffs. В режиме classic используются цены из .env.
    """
    from app.config import set_period_prices_from_db, settings

    # В режиме classic НЕ загружаем цены из тарифов - используем .env
    if settings.is_classic_mode():
        logger.info('Режим classic: цены периодов берутся из .env, тарифы игнорируются')
        return

    try:
        # Ищем тариф "Стандартный" или первый активный тариф
        result = await db.execute(
            select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.display_order, Tariff.id).limit(1)
        )
        tariff = result.scalar_one_or_none()

        if not tariff:
            logger.info('Активные тарифы не найдены, используются цены из .env')
            return

        if not tariff.period_prices:
            logger.warning("Тариф '%s' (id=%s) найден, но period_prices пуст", tariff.name, tariff.id)
            return

        # Преобразуем строковые ключи в int
        period_prices = {int(days): int(price) for days, price in tariff.period_prices.items() if int(price) > 0}

        if period_prices:
            set_period_prices_from_db(period_prices)
            logger.info(
                "Загружены периоды из тарифа '%s': %s",
                tariff.name,
                {f'{d}д': f'{p // 100}₽' for d, p in period_prices.items()},
            )
        else:
            logger.warning("Тариф '%s' не имеет активных периодов (все цены = 0)", tariff.name)

    except Exception as e:
        logger.error('Ошибка загрузки периодов из БД: %s', e)


async def ensure_tariffs_synced(db: AsyncSession) -> None:
    """
    Проверяет и синхронизирует тарифы при запуске.
    Вызывается при старте бота.
    """
    try:
        await sync_default_tariff_from_config(db)
        # Загружаем периоды из БД в PERIOD_PRICES
        await load_period_prices_from_db(db)
    except Exception as e:
        logger.error('Ошибка синхронизации тарифов: %s', e)
