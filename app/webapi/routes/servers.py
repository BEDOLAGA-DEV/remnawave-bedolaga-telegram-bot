"""Маршруты управления серверами в административном API."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.crud.server_squad import (
    create_server_squad,
    delete_server_squad,
    get_server_connected_users,
    get_server_squad_by_id,
    get_server_squad_by_uuid,
    get_server_statistics,
    sync_server_user_counts,
    sync_with_remnawave,
    update_server_squad,
    update_server_squad_promo_groups,
)
from app.database.models import PromoGroup, ServerSquad, User
from app.localization.texts import get_texts
from app.utils.cache import cache

from ..dependencies import get_db_session, require_api_token
from ..schemas.servers import (
    ServerConnectedUser,
    ServerConnectedUsersResponse,
    ServerCountsSyncResponse,
    ServerCreateRequest,
    ServerDeleteResponse,
    ServerListResponse,
    ServerResponse,
    ServerStatisticsResponse,
    ServerSyncResponse,
    ServerUpdateRequest,
)
from ..schemas.users import PromoGroupSummary


try:  # pragma: no cover - импорт может провалиться без optional-зависимостей
    from app.services.remnawave_service import RemnaWaveService  # type: ignore
except Exception:  # pragma: no cover - скрываем функционал, если сервис недоступен
    RemnaWaveService = None  # type: ignore[assignment]


if TYPE_CHECKING:  # pragma: no cover - только для подсказок типов в IDE
    from app.services.remnawave_service import (  # type: ignore
        RemnaWaveService as RemnaWaveServiceType,
    )
else:
    RemnaWaveServiceType = Any


router = APIRouter()



def _serialize_promo_group(group: PromoGroup) -> PromoGroupSummary:
    return PromoGroupSummary(
        id=group.id,
        name=group.name,
        server_discount_percent=group.server_discount_percent,
        traffic_discount_percent=group.traffic_discount_percent,
        device_discount_percent=group.device_discount_percent,
        apply_discounts_to_addons=getattr(group, 'apply_discounts_to_addons', True),
    )


def _serialize_server(server: ServerSquad) -> ServerResponse:
    promo_groups = [
        _serialize_promo_group(group)
        for group in sorted(
            getattr(server, 'allowed_promo_groups', []) or [],
            key=lambda pg: pg.name.lower() if getattr(pg, 'name', None) else '',
        )
    ]

    return ServerResponse(
        id=server.id,
        squad_uuid=server.squad_uuid,
        display_name=server.display_name,
        original_name=server.original_name,
        country_code=server.country_code,
        is_available=bool(server.is_available),
        is_trial_eligible=bool(server.is_trial_eligible),
        price_kopeks=int(server.price_kopeks or 0),
        price_rubles=round((server.price_kopeks or 0) / 100, 2),
        description=server.description,
        sort_order=int(server.sort_order or 0),
        max_users=server.max_users,
        current_users=int(server.current_users or 0),
        created_at=getattr(server, 'created_at', None),
        updated_at=getattr(server, 'updated_at', None),
        promo_groups=promo_groups,
    )


def _serialize_connected_user(user: User) -> ServerConnectedUser:
    subscription = getattr(user, 'subscription', None)
    subscription_status = getattr(subscription, 'status', None)
    if hasattr(subscription_status, 'value'):
        subscription_status = subscription_status.value

    return ServerConnectedUser(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        status=getattr(getattr(user, 'status', None), 'value', user.status),
        balance_kopeks=int(user.balance_kopeks or 0),
        balance_rubles=round((user.balance_kopeks or 0) / 100, 2),
        subscription_id=getattr(subscription, 'id', None),
        subscription_status=subscription_status,
        subscription_end_date=getattr(subscription, 'end_date', None),
    )


def _apply_filters(
    filters: Iterable[Any],
    query,
):
    for condition in filters:
        query = query.where(condition)
    return query


def _get_remnawave_service() -> RemnaWaveServiceType:
    texts = get_texts('ru')
    if RemnaWaveService is None:  # pragma: no cover - зависимость не доступна
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=texts.t(
                'WEBAPI_SERVERS_REMNAWAVE_SERVICE_UNAVAILABLE',
                'RemnaWave сервис недоступен',
            ),
        )

    return RemnaWaveService()


def _ensure_service_configured(service: RemnaWaveServiceType) -> None:
    texts = get_texts('ru')
    if RemnaWaveService is None:  # pragma: no cover - зависимость не доступна
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=texts.t(
                'WEBAPI_SERVERS_REMNAWAVE_SERVICE_UNAVAILABLE',
                'RemnaWave сервис недоступен',
            ),
        )

    if not service.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=service.configuration_error
            or texts.t(
                'WEBAPI_SERVERS_REMNAWAVE_API_NOT_CONFIGURED',
                'RemnaWave API не настроен',
            ),
        )


async def _validate_promo_group_ids(db: AsyncSession, promo_group_ids: Iterable[int]) -> list[int]:
    texts = get_texts('ru')
    unique_ids = [int(pg_id) for pg_id in set(promo_group_ids)]

    if not unique_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_SERVERS_PROMO_GROUP_REQUIRED',
                'Нужно выбрать хотя бы одну промогруппу',
            ),
        )

    result = await db.execute(select(PromoGroup.id).where(PromoGroup.id.in_(unique_ids)))
    found_ids = result.scalars().all()

    if not found_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_SERVERS_PROMO_GROUPS_NOT_FOUND_FOR_UPDATE',
                'Не найдены промогруппы для обновления сервера',
            ),
        )

    return unique_ids


@router.get('', response_model=ServerListResponse)
async def list_servers(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    available_only: bool = Query(False, alias='available'),
    search: str | None = Query(default=None),
) -> ServerListResponse:
    filters = []

    if available_only:
        filters.append(ServerSquad.is_available.is_(True))

    if search:
        pattern = f'%{search.lower()}%'
        filters.append(
            or_(
                func.lower(ServerSquad.display_name).like(pattern),
                func.lower(ServerSquad.original_name).like(pattern),
                func.lower(ServerSquad.squad_uuid).like(pattern),
                func.lower(ServerSquad.country_code).like(pattern),
            )
        )

    base_query = (
        select(ServerSquad)
        .options(selectinload(ServerSquad.allowed_promo_groups))
        .order_by(ServerSquad.sort_order, ServerSquad.display_name)
    )

    count_query = select(func.count(ServerSquad.id))

    if filters:
        base_query = _apply_filters(filters, base_query)
        count_query = _apply_filters(filters, count_query)

    total = await db.scalar(count_query) or 0

    result = await db.execute(base_query.offset((page - 1) * limit).limit(limit))
    servers = result.scalars().unique().all()

    return ServerListResponse(
        items=[_serialize_server(server) for server in servers],
        total=int(total),
        page=page,
        limit=limit,
    )


@router.get('/stats', response_model=ServerStatisticsResponse)
async def get_servers_statistics(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServerStatisticsResponse:
    stats = await get_server_statistics(db)

    return ServerStatisticsResponse(
        total_servers=int(stats.get('total_servers', 0) or 0),
        available_servers=int(stats.get('available_servers', 0) or 0),
        unavailable_servers=int(stats.get('unavailable_servers', 0) or 0),
        servers_with_connections=int(stats.get('servers_with_connections', 0) or 0),
        total_revenue_kopeks=int(stats.get('total_revenue_kopeks', 0) or 0),
        total_revenue_rubles=float(stats.get('total_revenue_rubles', 0) or 0),
    )


@router.post('', response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server_endpoint(
    payload: ServerCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServerResponse:
    texts = get_texts('ru')
    existing = await get_server_squad_by_uuid(db, payload.squad_uuid)
    if existing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_SERVERS_UUID_ALREADY_EXISTS',
                'Server with this UUID already exists',
            ),
        )

    try:
        server = await create_server_squad(
            db,
            squad_uuid=payload.squad_uuid,
            display_name=payload.display_name,
            original_name=payload.original_name,
            country_code=payload.country_code,
            price_kopeks=payload.price_kopeks,
            description=payload.description,
            max_users=payload.max_users,
            is_available=payload.is_available,
            is_trial_eligible=payload.is_trial_eligible,
            sort_order=payload.sort_order,
            promo_group_ids=payload.promo_group_ids,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    await cache.delete_pattern('available_countries*')

    server = await get_server_squad_by_id(db, server.id)
    assert server is not None
    return _serialize_server(server)


@router.get('/{server_id}', response_model=ServerResponse)
async def get_server_endpoint(
    server_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServerResponse:
    texts = get_texts('ru')
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_SERVERS_NOT_FOUND', 'Server not found'),
        )

    return _serialize_server(server)


@router.patch('/{server_id}', response_model=ServerResponse)
async def update_server_endpoint(
    server_id: int,
    payload: ServerUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServerResponse:
    texts = get_texts('ru')
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_SERVERS_NOT_FOUND', 'Server not found'),
        )

    updates = payload.model_dump(exclude_unset=True, by_alias=False)
    promo_group_ids = updates.pop('promo_group_ids', None)

    validated_promo_group_ids: list[int] | None = None
    if promo_group_ids is not None:
        validated_promo_group_ids = await _validate_promo_group_ids(db, promo_group_ids)

    if updates:
        server = await update_server_squad(db, server_id, **updates) or server

    if promo_group_ids is not None:
        try:
            assert validated_promo_group_ids is not None
            server = await update_server_squad_promo_groups(db, server_id, validated_promo_group_ids) or server
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    await cache.delete_pattern('available_countries*')

    server = await get_server_squad_by_id(db, server_id)
    assert server is not None
    return _serialize_server(server)


@router.delete('/{server_id}', response_model=ServerDeleteResponse)
async def delete_server_endpoint(
    server_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServerDeleteResponse:
    texts = get_texts('ru')
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_SERVERS_NOT_FOUND', 'Server not found'),
        )

    deleted = await delete_server_squad(db, server_id)
    if not deleted:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_SERVERS_DELETE_HAS_ACTIVE_CONNECTIONS',
                'Server cannot be deleted because it has active connections',
            ),
        )

    await cache.delete_pattern('available_countries*')

    return ServerDeleteResponse(
        success=True,
        message=texts.t('WEBAPI_SERVERS_DELETE_SUCCESS_MESSAGE', 'Server deleted'),
    )


@router.get(
    '/{server_id}/users',
    response_model=ServerConnectedUsersResponse,
)
async def get_server_connected_users_endpoint(
    server_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ServerConnectedUsersResponse:
    texts = get_texts('ru')
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_SERVERS_NOT_FOUND', 'Server not found'),
        )

    users = await get_server_connected_users(db, server_id)
    total = len(users)
    sliced = users[offset : offset + limit]

    return ServerConnectedUsersResponse(
        items=[_serialize_connected_user(user) for user in sliced],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('/sync', response_model=ServerSyncResponse)
async def sync_servers_with_remnawave(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServerSyncResponse:
    service = _get_remnawave_service()
    _ensure_service_configured(service)

    squads = await service.get_all_squads()
    total = len(squads)

    created = updated = removed = 0
    if squads:
        created, updated, removed = await sync_with_remnawave(db, squads)

    await cache.delete_pattern('available_countries*')

    return ServerSyncResponse(
        created=created,
        updated=updated,
        removed=removed,
        total=total,
    )


@router.post('/sync-counts', response_model=ServerCountsSyncResponse)
async def sync_server_counts(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> ServerCountsSyncResponse:
    updated = await sync_server_user_counts(db)
    return ServerCountsSyncResponse(updated=updated)
