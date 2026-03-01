"""Admin routes for managing servers in cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import (
    count_active_users_for_squad,
    get_all_server_squads,
    get_server_squad_by_id,
    sync_with_remnawave,
    update_server_squad,
    update_server_squad_promo_groups,
)
from app.database.models import PromoGroup, ServerSquad, Subscription, Tariff, User
from app.services.subscription_service import SubscriptionService

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.servers import (
    PromoGroupInfo,
    ServerDetailResponse,
    ServerListItem,
    ServerListResponse,
    ServerStatsResponse,
    ServerSyncResponse,
    ServerToggleResponse,
    ServerTrialToggleResponse,
    ServerUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/servers', tags=['Cabinet Admin Servers'])


async def _get_server_promo_groups(db: AsyncSession, server: ServerSquad) -> list[PromoGroupInfo]:
    """Get promo group info for server."""
    result = await db.execute(select(PromoGroup).order_by(PromoGroup.name))
    all_groups = result.scalars().all()

    selected_ids = {pg.id for pg in server.allowed_promo_groups} if server.allowed_promo_groups else set()

    return [
        PromoGroupInfo(
            id=pg.id,
            name=pg.name,
            is_selected=pg.id in selected_ids,
        )
        for pg in all_groups
    ]


async def _get_tariffs_using_server(db: AsyncSession, squad_uuid: str) -> list[str]:
    """Get list of tariff names using this server."""
    # Get all tariffs and filter in Python since JSON array queries are DB-specific
    result = await db.execute(select(Tariff.name, Tariff.allowed_squads))
    tariff_names = []
    for name, allowed_squads in result.fetchall():
        if allowed_squads and squad_uuid in allowed_squads:
            tariff_names.append(name)
    return tariff_names


@router.get('', response_model=ServerListResponse)
async def list_servers(
    include_unavailable: bool = True,
    admin: User = Depends(require_permission('servers:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of all servers."""
    servers, total = await get_all_server_squads(
        db,
        available_only=not include_unavailable,
    )

    items = []
    for server in servers:
        items.append(
            ServerListItem(
                id=server.id,
                squad_uuid=server.squad_uuid,
                display_name=server.display_name,
                original_name=server.original_name,
                country_code=server.country_code,
                is_available=server.is_available,
                is_trial_eligible=server.is_trial_eligible,
                price_kopeks=server.price_kopeks,
                price_rubles=server.price_kopeks / 100,
                max_users=server.max_users,
                current_users=server.current_users or 0,
                sort_order=server.sort_order,
                is_full=server.is_full,
                availability_status=server.availability_status,
                created_at=server.created_at,
            )
        )

    return ServerListResponse(servers=items, total=total)


@router.get('/{server_id}', response_model=ServerDetailResponse)
async def get_server(
    server_id: int,
    admin: User = Depends(require_permission('servers:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed server info."""
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Server not found',
        )

    promo_groups = await _get_server_promo_groups(db, server)
    tariffs_using = await _get_tariffs_using_server(db, server.squad_uuid)
    active_subs = await count_active_users_for_squad(db, server.squad_uuid)

    return ServerDetailResponse(
        id=server.id,
        squad_uuid=server.squad_uuid,
        display_name=server.display_name,
        original_name=server.original_name,
        country_code=server.country_code,
        description=server.description,
        is_available=server.is_available,
        is_trial_eligible=server.is_trial_eligible,
        price_kopeks=server.price_kopeks,
        price_rubles=server.price_kopeks / 100,
        max_users=server.max_users,
        current_users=server.current_users or 0,
        sort_order=server.sort_order,
        is_full=server.is_full,
        availability_status=server.availability_status,
        promo_groups=promo_groups,
        active_subscriptions=active_subs,
        tariffs_using=tariffs_using,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


@router.put('/{server_id}', response_model=ServerDetailResponse)
async def update_existing_server(
    server_id: int,
    request: ServerUpdateRequest,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update an existing server."""
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Server not found',
        )

    # Build updates dict
    updates = {}
    if request.display_name is not None:
        updates['display_name'] = request.display_name
    if request.description is not None:
        updates['description'] = request.description
    if request.country_code is not None:
        updates['country_code'] = request.country_code
    if request.is_available is not None:
        updates['is_available'] = request.is_available
    if request.is_trial_eligible is not None:
        updates['is_trial_eligible'] = request.is_trial_eligible
    if request.price_kopeks is not None:
        updates['price_kopeks'] = request.price_kopeks
    if request.max_users is not None:
        updates['max_users'] = request.max_users if request.max_users > 0 else None
    if request.sort_order is not None:
        updates['sort_order'] = request.sort_order

    if updates:
        await update_server_squad(db, server_id, **updates)

    # Update promo groups separately
    if request.promo_group_ids is not None:
        await update_server_squad_promo_groups(db, server_id, request.promo_group_ids)

    logger.info('Admin updated server', admin_id=admin.id, server_id=server_id)

    return await get_server(server_id, admin, db)


@router.post('/{server_id}/toggle', response_model=ServerToggleResponse)
async def toggle_server(
    server_id: int,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle server availability."""
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Server not found',
        )

    new_status = not server.is_available
    await update_server_squad(db, server_id, is_available=new_status)

    status_text = 'enabled' if new_status else 'disabled'
    logger.info('Admin server', admin_id=admin.id, status_text=status_text, server_id=server_id)

    return ServerToggleResponse(
        id=server_id,
        is_available=new_status,
        message=f'Server {status_text}',
    )


@router.post('/{server_id}/trial', response_model=ServerTrialToggleResponse)
async def toggle_server_trial(
    server_id: int,
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle server trial eligibility."""
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Server not found',
        )

    new_status = not server.is_trial_eligible
    await update_server_squad(db, server_id, is_trial_eligible=new_status)

    status_text = 'enabled for trial' if new_status else 'disabled for trial'
    logger.info('Admin server', admin_id=admin.id, status_text=status_text, server_id=server_id)

    return ServerTrialToggleResponse(
        id=server_id,
        is_trial_eligible=new_status,
        message=f'Server {status_text}',
    )


@router.get('/{server_id}/stats', response_model=ServerStatsResponse)
async def get_server_stats(
    server_id: int,
    admin: User = Depends(require_permission('servers:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get server statistics."""
    server = await get_server_squad_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Server not found',
        )

    active_subs = await count_active_users_for_squad(db, server.squad_uuid)

    # Count trial subscriptions on this server
    # Use LIKE query for JSON array since .contains() is DB-specific
    trial_result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.is_trial == True,
            Subscription.status == 'active',
            func.cast(Subscription.connected_squads, String).like(f'%"{server.squad_uuid}"%'),
        )
    )
    trial_count = trial_result.scalar() or 0

    usage_percent = None
    if server.max_users and server.max_users > 0:
        usage_percent = round((server.current_users or 0) / server.max_users * 100, 1)

    return ServerStatsResponse(
        id=server_id,
        display_name=server.display_name,
        squad_uuid=server.squad_uuid,
        current_users=server.current_users or 0,
        max_users=server.max_users,
        active_subscriptions=active_subs,
        trial_subscriptions=trial_count,
        usage_percent=usage_percent,
    )


@router.post('/sync', response_model=ServerSyncResponse)
async def sync_servers(
    admin: User = Depends(require_permission('servers:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Sync servers with RemnaWave."""
    try:
        subscription_service = SubscriptionService()
        if not subscription_service.is_configured:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='RemnaWave is not configured',
            )

        # Get squads from RemnaWave
        squads = await subscription_service.get_remnawave_squads()
        if squads is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to fetch squads from RemnaWave',
            )

        # Sync with database
        created, updated, removed = await sync_with_remnawave(db, squads)

        logger.info('Admin synced servers: + ~', admin_id=admin.id, created=created, updated=updated, removed=removed)

        return ServerSyncResponse(
            created=created,
            updated=updated,
            removed=removed,
            message=f'Synced: {created} created, {updated} updated, {removed} removed',
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to sync servers', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Sync failed: {e!s}',
        )
