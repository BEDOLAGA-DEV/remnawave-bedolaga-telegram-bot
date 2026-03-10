import random
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import (
    String,
    and_,
    cast,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    PromoGroup,
    ServerSquad,
    Subscription,
    SubscriptionServer,
    SubscriptionStatus,
    Tariff,
    User,
)


logger = structlog.get_logger(__name__)


async def _get_default_promo_group_id(db: AsyncSession) -> int | None:
    result = await db.execute(select(PromoGroup.id).where(PromoGroup.is_default.is_(True)).limit(1))
    return result.scalar_one_or_none()


async def create_server_squad(
    db: AsyncSession,
    squad_uuid: str,
    display_name: str,
    original_name: str = None,
    country_code: str = None,
    price_kopeks: int = 0,
    description: str = None,
    max_users: int = None,
    is_available: bool = True,
    is_trial_eligible: bool = False,
    sort_order: int = 0,
    promo_group_ids: Iterable[int] | None = None,
) -> ServerSquad:
    normalized_group_ids: Sequence[int]
    if promo_group_ids is None:
        default_id = await _get_default_promo_group_id(db)
        normalized_group_ids = [default_id] if default_id is not None else []
    else:
        normalized_group_ids = [int(pg_id) for pg_id in set(promo_group_ids)]

    if not normalized_group_ids:
        raise ValueError('Server squad must be linked to at least one promo group')

    promo_groups_result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(normalized_group_ids)))
    promo_groups = promo_groups_result.scalars().all()

    if len(promo_groups) != len(normalized_group_ids):
        logger.warning('Не все промогруппы найдены при создании сервера', display_name=display_name)

    server_squad = ServerSquad(
        squad_uuid=squad_uuid,
        display_name=display_name,
        original_name=original_name,
        country_code=country_code,
        price_kopeks=price_kopeks,
        description=description,
        max_users=max_users,
        is_available=is_available,
        is_trial_eligible=is_trial_eligible,
        sort_order=sort_order,
        allowed_promo_groups=promo_groups,
    )

    db.add(server_squad)
    await db.commit()
    await db.refresh(server_squad)

    logger.info('✅ Создан сервер (UUID: )', display_name=display_name, squad_uuid=squad_uuid)
    return server_squad


async def get_server_squad_by_uuid(db: AsyncSession, squad_uuid: str) -> ServerSquad | None:
    result = await db.execute(
        select(ServerSquad)
        .options(selectinload(ServerSquad.allowed_promo_groups))
        .where(ServerSquad.squad_uuid == squad_uuid)
    )
    return result.scalars().unique().one_or_none()


async def get_server_squad_by_id(db: AsyncSession, server_id: int) -> ServerSquad | None:
    result = await db.execute(
        select(ServerSquad).options(selectinload(ServerSquad.allowed_promo_groups)).where(ServerSquad.id == server_id)
    )
    return result.scalars().unique().one_or_none()


async def get_all_server_squads(
    db: AsyncSession, available_only: bool = False, page: int = 1, limit: int = 50
) -> tuple[list[ServerSquad], int]:
    query = select(ServerSquad)

    if available_only:
        query = query.where(ServerSquad.is_available == True)

    count_query = select(func.count(ServerSquad.id))
    if available_only:
        count_query = count_query.where(ServerSquad.is_available == True)

    count_result = await db.execute(count_query)
    total_count = count_result.scalar()

    offset = (page - 1) * limit
    query = query.order_by(ServerSquad.sort_order, ServerSquad.display_name)
    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    servers = result.scalars().all()

    return servers, total_count


async def get_available_server_squads(
    db: AsyncSession,
    promo_group_id: int | None = None,
    exclude_trial_only: bool = False,
) -> list[ServerSquad]:
    query = (
        select(ServerSquad)
        .options(selectinload(ServerSquad.allowed_promo_groups))
        .where(ServerSquad.is_available.is_(True))
        .order_by(ServerSquad.sort_order, ServerSquad.display_name)
    )

    if exclude_trial_only:
        query = query.where(ServerSquad.is_trial_eligible.is_(False))

    if promo_group_id is not None:
        query = query.join(ServerSquad.allowed_promo_groups).where(PromoGroup.id == promo_group_id)

    result = await db.execute(query)
    return result.scalars().unique().all()


async def get_active_server_squads(db: AsyncSession) -> list[ServerSquad]:
    """Возвращает список активных серверов, доступных для подключения."""

    squads = await get_available_server_squads(db)

    if not squads:
        return []

    eligible: list[ServerSquad] = []

    for squad in squads:
        max_users = squad.max_users
        current_users = squad.current_users or 0

        if max_users is not None and current_users >= max_users:
            continue

        eligible.append(squad)

    if eligible:
        return eligible

    return squads


async def choose_random_active_server_squad(
    db: AsyncSession,
) -> ServerSquad | None:
    """Возвращает случайный активный сервер."""

    squads = await get_active_server_squads(db)

    if not squads:
        return None

    return random.choice(squads)


async def get_random_active_squad_uuid(
    db: AsyncSession,
    fallback_uuid: str | None = None,
) -> str | None:
    """Возвращает UUID случайного активного сервера или запасной UUID."""

    squad = await choose_random_active_server_squad(db)

    if squad:
        return squad.squad_uuid

    return fallback_uuid


async def update_server_squad_promo_groups(
    db: AsyncSession, server_id: int, promo_group_ids: Iterable[int]
) -> ServerSquad | None:
    unique_ids = [int(pg_id) for pg_id in set(promo_group_ids)]

    if not unique_ids:
        raise ValueError('Нужно выбрать хотя бы одну промогруппу')

    server = await get_server_squad_by_id(db, server_id)
    if not server:
        return None

    result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(unique_ids)))
    promo_groups = result.scalars().all()

    if not promo_groups:
        raise ValueError('Не найдены промогруппы для обновления сервера')

    server.allowed_promo_groups = promo_groups
    await db.commit()
    await db.refresh(server)

    logger.info(
        'Обновлены промогруппы сервера %s (ID: %s): %s',
        server.display_name,
        server.id,
        ', '.join(pg.name for pg in promo_groups),
    )

    return server


async def update_server_squad(db: AsyncSession, server_id: int, **updates) -> ServerSquad | None:
    valid_fields = {
        'display_name',
        'original_name',
        'country_code',
        'price_kopeks',
        'description',
        'max_users',
        'is_available',
        'sort_order',
        'is_trial_eligible',
    }

    filtered_updates = {k: v for k, v in updates.items() if k in valid_fields}

    if not filtered_updates:
        return None

    await db.execute(update(ServerSquad).where(ServerSquad.id == server_id).values(**filtered_updates))

    await db.commit()

    return await get_server_squad_by_id(db, server_id)


async def delete_server_squad(db: AsyncSession, server_id: int) -> bool:
    connections_result = await db.execute(
        select(func.count(SubscriptionServer.id)).where(SubscriptionServer.server_squad_id == server_id)
    )
    connections_count = connections_result.scalar()

    if connections_count > 0:
        logger.warning(
            '⚠ Нельзя удалить сервер есть активные подключения',
            server_id=server_id,
            connections_count=connections_count,
        )
        return False

    await db.execute(delete(ServerSquad).where(ServerSquad.id == server_id))
    await db.commit()

    logger.info('🗑️ Удален сервер (ID: )', server_id=server_id)
    return True


async def sync_with_remnawave(db: AsyncSession, remnawave_squads: list[dict]) -> tuple[int, int, int]:
    created = 0
    updated = 0
    removed = 0

    existing_servers = {}
    result = await db.execute(select(ServerSquad))
    for server in result.scalars().all():
        existing_servers[server.squad_uuid] = server

    remnawave_uuids = {squad['uuid'] for squad in remnawave_squads}

    for squad in remnawave_squads:
        squad_uuid = squad['uuid']
        original_name = squad.get('name', f'Squad {squad_uuid[:8]}')

        if squad_uuid in existing_servers:
            server = existing_servers[squad_uuid]
            if server.original_name != original_name:
                server.original_name = original_name
                updated += 1
        else:
            await create_server_squad(
                db=db,
                squad_uuid=squad_uuid,
                display_name=original_name,
                original_name=original_name,
                price_kopeks=1000,
                is_available=False,
            )
            created += 1

    # Protect external squads referenced by tariffs from being removed during sync
    tariff_ext_uuids_result = await db.execute(
        select(Tariff.external_squad_uuid).where(Tariff.external_squad_uuid.isnot(None))
    )
    protected_uuids = {row[0] for row in tariff_ext_uuids_result.fetchall()}

    removed_servers = [
        server for uuid, server in existing_servers.items()
        if uuid not in remnawave_uuids and uuid not in protected_uuids
    ]

    if removed_servers:
        removed_ids = [server.id for server in removed_servers]
        removed_uuids = {server.squad_uuid for server in removed_servers}

        subscription_ids_result = await db.execute(
            select(SubscriptionServer.subscription_id).where(SubscriptionServer.server_squad_id.in_(removed_ids))
        )
        subscription_ids = {row[0] for row in subscription_ids_result.fetchall()}

        for server in removed_servers:
            logger.info('🗑️ Удаляется сервер (UUID:)', display_name=server.display_name, squad_uuid=server.squad_uuid)

        await db.execute(delete(SubscriptionServer).where(SubscriptionServer.server_squad_id.in_(removed_ids)))

        subscriptions_to_update: dict[int, Subscription] = {}

        if subscription_ids:
            subscriptions_result = await db.execute(select(Subscription).where(Subscription.id.in_(subscription_ids)))
            for subscription in subscriptions_result.scalars().unique().all():
                subscriptions_to_update[subscription.id] = subscription

        for squad_uuid in removed_uuids:
            if not squad_uuid:
                continue

            extra_result = await db.execute(
                select(Subscription).where(text('connected_squads::text LIKE :uuid_pattern')),
                {'uuid_pattern': f'%"{squad_uuid}"%'},
            )

            for subscription in extra_result.scalars().unique().all():
                subscriptions_to_update[subscription.id] = subscription

        cleaned_subscriptions = 0

        for subscription in subscriptions_to_update.values():
            current_squads = list(subscription.connected_squads or [])
            if not current_squads:
                continue

            filtered_squads = [squad_uuid for squad_uuid in current_squads if squad_uuid not in removed_uuids]

            if len(filtered_squads) != len(current_squads):
                subscription.connected_squads = filtered_squads
                subscription.updated_at = datetime.now(UTC)
                cleaned_subscriptions += 1

        # Clean up stale UUIDs from tariff allowed_squads
        cleaned_tariffs = 0
        tariffs_result = await db.execute(select(Tariff))
        for tariff in tariffs_result.scalars().all():
            current = list(tariff.allowed_squads or [])
            if not current:
                continue
            filtered = [u for u in current if u not in removed_uuids]
            if len(filtered) != len(current):
                tariff.allowed_squads = filtered
                tariff.updated_at = datetime.now(UTC)
                cleaned_tariffs += 1
                logger.info(
                    '🧹 Тариф "%s" (ID: %s): удалены несуществующие сквады %s',
                    tariff.name,
                    tariff.id,
                    [u for u in current if u in removed_uuids],
                )

        await db.execute(delete(ServerSquad).where(ServerSquad.id.in_(removed_ids)))
        removed = len(removed_servers)

        if cleaned_subscriptions:
            logger.info('🧹 Обновлены подписки после удаления серверов', cleaned_subscriptions=cleaned_subscriptions)

        if cleaned_tariffs:
            logger.info('🧹 Обновлены тарифы после удаления серверов', cleaned_tariffs=cleaned_tariffs)

    await db.commit()

    logger.info('🔄 Синхронизация завершена: + ~', created=created, updated=updated, removed=removed)
    return created, updated, removed


async def get_server_connected_users(db: AsyncSession, server_id: int) -> list[User]:
    server_uuid_result = await db.execute(select(ServerSquad.squad_uuid).where(ServerSquad.id == server_id))
    server_uuid = server_uuid_result.scalar_one_or_none()

    connection_filters = [SubscriptionServer.id.isnot(None)]

    if server_uuid:
        connection_filters.append(cast(Subscription.connected_squads, String).like(f'%"{server_uuid}"%'))

    result = await db.execute(
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .outerjoin(
            SubscriptionServer,
            and_(
                SubscriptionServer.subscription_id == Subscription.id,
                SubscriptionServer.server_squad_id == server_id,
            ),
        )
        .where(or_(*connection_filters))
        .options(selectinload(User.subscription))
        .order_by(User.id)
    )

    return result.scalars().unique().all()


async def get_trial_eligible_server_squads(
    db: AsyncSession,
    include_unavailable: bool = False,
) -> list[ServerSquad]:
    query = select(ServerSquad).where(ServerSquad.is_trial_eligible.is_(True))

    result = await db.execute(query)
    squads = result.scalars().unique().all()

    if include_unavailable:
        return squads

    preferred_squads: list[ServerSquad] = []
    fallback_squads: list[ServerSquad] = []

    for squad in squads:
        current_users = squad.current_users or 0
        is_full = squad.max_users is not None and current_users >= squad.max_users

        if is_full:
            continue

        if squad.is_available:
            preferred_squads.append(squad)
        else:
            fallback_squads.append(squad)

    if preferred_squads:
        return preferred_squads

    if fallback_squads:
        return fallback_squads

    return squads


async def choose_random_trial_server_squad(
    db: AsyncSession,
) -> ServerSquad | None:
    squads = await get_trial_eligible_server_squads(db)

    if not squads:
        return None

    return random.choice(squads)


async def get_random_trial_squad_uuid(
    db: AsyncSession,
) -> str | None:
    squad = await choose_random_trial_server_squad(db)

    if squad:
        return squad.squad_uuid

    return None


async def get_server_statistics(db: AsyncSession) -> dict:
    total_result = await db.execute(select(func.count(ServerSquad.id)))
    total_servers = total_result.scalar()

    available_result = await db.execute(select(func.count(ServerSquad.id)).where(ServerSquad.is_available == True))
    available_servers = available_result.scalar()

    servers_with_connections = 0
    all_servers_result = await db.execute(select(ServerSquad.squad_uuid))
    all_server_uuids = [row[0] for row in all_servers_result.fetchall()]

    for squad_uuid in all_server_uuids:
        count_result = await db.execute(
            text("""
                SELECT COUNT(s.id)
                FROM subscriptions s
                WHERE s.status IN ('active', 'trial')
                AND s.connected_squads::text LIKE :uuid_pattern
            """),
            {'uuid_pattern': f'%"{squad_uuid}"%'},
        )
        user_count = count_result.scalar() or 0
        if user_count > 0:
            servers_with_connections += 1

    revenue_result = await db.execute(select(func.coalesce(func.sum(SubscriptionServer.paid_price_kopeks), 0)))
    total_revenue_kopeks = revenue_result.scalar()

    return {
        'total_servers': total_servers,
        'available_servers': available_servers,
        'unavailable_servers': total_servers - available_servers,
        'servers_with_connections': servers_with_connections,
        'total_revenue_kopeks': total_revenue_kopeks,
        'total_revenue_rubles': total_revenue_kopeks / 100,
    }


async def count_active_users_for_squad(db: AsyncSession, squad_uuid: str) -> int:
    """Возвращает количество активных подписок, подключенных к указанному скваду."""

    result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.status.in_(
                [
                    SubscriptionStatus.ACTIVE.value,
                    SubscriptionStatus.TRIAL.value,
                ]
            ),
            cast(Subscription.connected_squads, String).like(f'%"{squad_uuid}"%'),
        )
    )

    return result.scalar() or 0


async def add_user_to_servers(db: AsyncSession, server_squad_ids: list[int]) -> bool:
    try:
        for server_id in sorted(server_squad_ids):
            await db.execute(
                update(ServerSquad)
                .where(ServerSquad.id == server_id)
                .values(current_users=ServerSquad.current_users + 1)
            )

        await db.flush()
        logger.info('✅ Увеличен счетчик пользователей для серверов', server_squad_ids=server_squad_ids)
        return True

    except Exception as e:
        logger.error('Ошибка увеличения счетчика пользователей', error=e)
        raise


async def remove_user_from_servers(db: AsyncSession, server_squad_ids: list[int]) -> bool:
    try:
        for server_id in sorted(server_squad_ids):
            await db.execute(
                update(ServerSquad)
                .where(ServerSquad.id == server_id)
                .values(current_users=func.greatest(ServerSquad.current_users - 1, 0))
            )

        await db.flush()
        logger.info('✅ Уменьшен счетчик пользователей для серверов', server_squad_ids=server_squad_ids)
        return True

    except Exception as e:
        logger.error('Ошибка уменьшения счетчика пользователей', error=e)
        raise


async def update_server_user_counts(
    db: AsyncSession,
    add_ids: list[int] | None = None,
    remove_ids: list[int] | None = None,
) -> None:
    """Increment and decrement server user counters in a single sorted pass.

    Prevents deadlocks by acquiring row locks in consistent ID order
    across both add and remove operations within one transaction.
    """
    try:
        add_set = set(add_ids) if add_ids else set()
        remove_set = set(remove_ids) if remove_ids else set()

        if not add_set and not remove_set:
            return

        # IDs in both sets cancel out — skip them
        overlap = add_set & remove_set
        if overlap:
            add_set -= overlap
            remove_set -= overlap

        all_ids = sorted(add_set | remove_set)
        if not all_ids:
            return

        for server_id in all_ids:
            if server_id in add_set:
                await db.execute(
                    update(ServerSquad)
                    .where(ServerSquad.id == server_id)
                    .values(current_users=ServerSquad.current_users + 1)
                )
            if server_id in remove_set:
                await db.execute(
                    update(ServerSquad)
                    .where(ServerSquad.id == server_id)
                    .values(current_users=func.greatest(ServerSquad.current_users - 1, 0))
                )

        await db.flush()
        if add_set:
            logger.info('✅ Увеличен счетчик пользователей для серверов', sorted=sorted(add_set))
        if remove_set:
            logger.info('✅ Уменьшен счетчик пользователей для серверов', sorted=sorted(remove_set))

    except Exception as e:
        logger.error('Ошибка обновления счетчиков серверов', e=e)
        raise


async def get_server_ids_by_uuids(db: AsyncSession, squad_uuids: list[str]) -> list[int]:
    result = await db.execute(select(ServerSquad.id).where(ServerSquad.squad_uuid.in_(squad_uuids)))
    return [row[0] for row in result.fetchall()]


async def get_server_squads_by_uuids(db: AsyncSession, squad_uuids: list[str]) -> list[ServerSquad]:
    """Получает список ServerSquad объектов по их UUID с загрузкой allowed_promo_groups."""
    if not squad_uuids:
        return []

    result = await db.execute(
        select(ServerSquad)
        .options(selectinload(ServerSquad.allowed_promo_groups))
        .where(ServerSquad.squad_uuid.in_(squad_uuids))
    )
    return list(result.scalars().all())


async def ensure_servers_synced(db: AsyncSession) -> None:
    """
    Проверяет и синхронизирует серверы при запуске.
    Если серверов нет в БД, загружает их из RemnaWave.
    Вызывается при старте бота.
    """
    try:
        # Проверяем есть ли серверы в БД
        result = await db.execute(select(func.count(ServerSquad.id)))
        server_count = result.scalar() or 0

        if server_count > 0:
            logger.info('✅ В базе уже есть серверов, пропускаем синхронизацию', server_count=server_count)
            return

        logger.info('🔄 Серверов в БД нет, начинаем синхронизацию с RemnaWave...')

        # Импортируем сервис здесь чтобы избежать циклических импортов
        from app.services.subscription_service import SubscriptionService

        subscription_service = SubscriptionService()
        if not subscription_service.is_configured:
            logger.warning('⚠️ RemnaWave не настроен, серверы не синхронизированы')
            return

        # Получаем скводы из RemnaWave
        squads = await subscription_service.get_remnawave_squads()
        if squads is None:
            logger.error('❌ Не удалось получить список серверов из RemnaWave')
            return

        if not squads:
            logger.warning('⚠️ RemnaWave вернул пустой список серверов')
            return

        # Синхронизируем
        created, updated, removed = await sync_with_remnawave(db, squads)
        logger.info('✅ Серверы синхронизированы: + ~', created=created, updated=updated, removed=removed)

    except Exception as e:
        logger.error('❌ Ошибка синхронизации серверов', error=e)


async def sync_server_user_counts(db: AsyncSession) -> int:
    try:
        all_servers_result = await db.execute(select(ServerSquad.id, ServerSquad.squad_uuid))
        all_servers = all_servers_result.fetchall()

        logger.info('🔍 Найдено серверов для синхронизации', all_servers_count=len(all_servers))

        updated_count = 0
        for server_id, squad_uuid in all_servers:
            count_result = await db.execute(
                text("""
                    SELECT COUNT(s.id)
                    FROM subscriptions s
                    WHERE s.status IN ('active', 'trial')
                    AND s.connected_squads::text LIKE :uuid_pattern
                """),
                {'uuid_pattern': f'%"{squad_uuid}"%'},
            )
            actual_users = count_result.scalar() or 0

            logger.info(
                '📊 Сервер пользователей', server_id=server_id, squad_uuid=squad_uuid[:8], actual_users=actual_users
            )

            await db.execute(update(ServerSquad).where(ServerSquad.id == server_id).values(current_users=actual_users))
            updated_count += 1

        await db.commit()
        logger.info('✅ Синхронизированы счетчики для серверов', updated_count=updated_count)
        return updated_count

    except Exception as e:
        logger.error('Ошибка синхронизации счетчиков пользователей', error=e)
        await db.rollback()
        return 0
