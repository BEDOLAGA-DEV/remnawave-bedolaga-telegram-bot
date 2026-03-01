"""Admin routes for RemnaWave management in cabinet."""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import (
    count_active_users_for_squad,
    get_all_server_squads,
    get_server_squad_by_uuid,
    sync_with_remnawave,
)
from app.database.models import User
from app.utils.cache import cache

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.remnawave import (
    AutoSyncRunResponse,
    # Auto Sync
    AutoSyncStatus,
    AutoSyncToggleRequest,
    Bandwidth,
    ConnectionStatus,
    # Inbounds
    InboundsListResponse,
    # Migration
    MigrationPreviewResponse,
    MigrationRequest,
    MigrationResponse,
    MigrationStats,
    NodeActionRequest,
    NodeActionResponse,
    # Nodes
    NodeInfo,
    NodesListResponse,
    NodesOverview,
    NodeStatisticsResponse,
    NodeUsageResponse,
    # Status & Connection
    RemnaWaveStatusResponse,
    ServerInfo,
    SquadActionRequest,
    SquadCreateRequest,
    SquadDetailResponse,
    SquadOperationResponse,
    SquadsListResponse,
    SquadUpdateRequest,
    # Squads
    SquadWithLocalInfo,
    # Manual Sync
    SyncMode,
    SyncResponse,
    # System Statistics
    SystemStatsResponse,
    SystemSummary,
    TrafficPeriod,
    TrafficPeriods,
)


try:
    from app.services.remnawave_service import (
        RemnaWaveConfigurationError,
        RemnaWaveService,
    )
except Exception:
    RemnaWaveConfigurationError = None
    RemnaWaveService = None

try:
    from app.services.remnawave_sync_service import remnawave_sync_service
except Exception:
    remnawave_sync_service = None


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/remnawave', tags=['Cabinet Admin RemnaWave'])


# ============ Helpers ============


def _get_service() -> RemnaWaveService:
    """Get RemnaWave service instance."""
    if RemnaWaveService is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='RemnaWave service is not available',
        )
    return RemnaWaveService()


def _ensure_configured(service: RemnaWaveService) -> None:
    """Ensure RemnaWave is configured."""
    if not service.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=service.configuration_error or 'RemnaWave API is not configured',
        )


def _parse_datetime(value: Any) -> datetime | None:
    """Parse datetime from various formats."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            return None
    return None


def _serialize_node(node_data: dict[str, Any]) -> NodeInfo:
    """Serialize node data to NodeInfo model."""
    return NodeInfo(
        uuid=node_data.get('uuid', ''),
        name=node_data.get('name', ''),
        address=node_data.get('address', ''),
        country_code=node_data.get('country_code'),
        is_connected=bool(node_data.get('is_connected')),
        is_disabled=bool(node_data.get('is_disabled')),
        is_node_online=bool(node_data.get('is_node_online')),
        is_xray_running=bool(node_data.get('is_xray_running')),
        users_online=node_data.get('users_online'),
        traffic_used_bytes=node_data.get('traffic_used_bytes'),
        traffic_limit_bytes=node_data.get('traffic_limit_bytes'),
        last_status_change=_parse_datetime(node_data.get('last_status_change')),
        last_status_message=node_data.get('last_status_message'),
        xray_uptime=node_data.get('xray_uptime'),
        is_traffic_tracking_active=bool(node_data.get('is_traffic_tracking_active', False)),
        traffic_reset_day=node_data.get('traffic_reset_day'),
        notify_percent=node_data.get('notify_percent'),
        consumption_multiplier=float(node_data.get('consumption_multiplier', 1.0)),
        cpu_count=node_data.get('cpu_count'),
        cpu_model=node_data.get('cpu_model'),
        total_ram=node_data.get('total_ram'),
        created_at=_parse_datetime(node_data.get('created_at')),
        updated_at=_parse_datetime(node_data.get('updated_at')),
        provider_uuid=node_data.get('provider_uuid'),
    )


# ============ Status & Connection ============


@router.get('/status', response_model=RemnaWaveStatusResponse)
async def get_remnawave_status(
    admin: User = Depends(require_permission('remnawave:read')),
) -> RemnaWaveStatusResponse:
    """Get RemnaWave configuration and connection status."""
    service = _get_service()

    connection_info: ConnectionStatus | None = None
    connection_result = await service.test_api_connection()

    if connection_result:
        connection_info = ConnectionStatus(**connection_result)

    return RemnaWaveStatusResponse(
        is_configured=service.is_configured,
        configuration_error=service.configuration_error,
        connection=connection_info,
    )


# ============ System Statistics ============


@router.get('/system', response_model=SystemStatsResponse)
async def get_system_statistics(
    admin: User = Depends(require_permission('remnawave:read')),
) -> SystemStatsResponse:
    """Get full system statistics from RemnaWave."""
    service = _get_service()
    _ensure_configured(service)

    stats = await service.get_system_statistics()
    if not stats or 'system' not in stats:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Failed to get RemnaWave statistics',
        )

    system_data = stats.get('system', {})
    server_data = stats.get('server_info', {})
    bandwidth_data = stats.get('bandwidth', {})
    traffic_data = stats.get('traffic_periods', {})

    return SystemStatsResponse(
        system=SystemSummary(
            users_online=system_data.get('users_online', 0),
            total_users=system_data.get('total_users', 0),
            active_connections=system_data.get('active_connections', 0),
            nodes_online=system_data.get('nodes_online', 0),
            users_last_day=system_data.get('users_last_day', 0),
            users_last_week=system_data.get('users_last_week', 0),
            users_never_online=system_data.get('users_never_online', 0),
            total_user_traffic=system_data.get('total_user_traffic', 0),
        ),
        users_by_status=stats.get('users_by_status', {}),
        server_info=ServerInfo(
            cpu_cores=server_data.get('cpu_cores', 0),
            cpu_physical_cores=server_data.get('cpu_physical_cores', 0),
            memory_total=server_data.get('memory_total', 0),
            memory_used=server_data.get('memory_used', 0),
            memory_free=server_data.get('memory_free', 0),
            memory_available=server_data.get('memory_available', 0),
            uptime_seconds=server_data.get('uptime_seconds', 0),
        ),
        bandwidth=Bandwidth(
            realtime_download=bandwidth_data.get('realtime_download', 0),
            realtime_upload=bandwidth_data.get('realtime_upload', 0),
            realtime_total=bandwidth_data.get('realtime_total', 0),
        ),
        traffic_periods=TrafficPeriods(
            last_2_days=TrafficPeriod(**traffic_data.get('last_2_days', {'current': 0, 'previous': 0})),
            last_7_days=TrafficPeriod(**traffic_data.get('last_7_days', {'current': 0, 'previous': 0})),
            last_30_days=TrafficPeriod(**traffic_data.get('last_30_days', {'current': 0, 'previous': 0})),
            current_month=TrafficPeriod(**traffic_data.get('current_month', {'current': 0, 'previous': 0})),
            current_year=TrafficPeriod(**traffic_data.get('current_year', {'current': 0, 'previous': 0})),
        ),
        nodes_realtime=stats.get('nodes_realtime', []),
        nodes_weekly=stats.get('nodes_weekly', []),
        last_updated=_parse_datetime(stats.get('last_updated')),
    )


# ============ Nodes ============


@router.get('/nodes', response_model=NodesListResponse)
async def list_nodes(
    admin: User = Depends(require_permission('remnawave:read')),
) -> NodesListResponse:
    """Get list of all nodes."""
    service = _get_service()
    _ensure_configured(service)

    nodes = await service.get_all_nodes()
    serialized = [_serialize_node(node) for node in nodes]

    return NodesListResponse(items=serialized, total=len(serialized))


@router.get('/nodes/overview', response_model=NodesOverview)
async def get_nodes_overview(
    admin: User = Depends(require_permission('remnawave:read')),
) -> NodesOverview:
    """Get nodes overview with statistics."""
    service = _get_service()
    _ensure_configured(service)

    nodes = await service.get_all_nodes()

    total = len(nodes)
    online = sum(1 for n in nodes if n.get('is_connected') and not n.get('is_disabled'))
    disabled = sum(1 for n in nodes if n.get('is_disabled'))
    offline = total - online - disabled
    total_users_online = sum(n.get('users_online', 0) or 0 for n in nodes)

    return NodesOverview(
        total=total,
        online=online,
        offline=offline,
        disabled=disabled,
        total_users_online=total_users_online,
        nodes=[_serialize_node(n) for n in nodes],
    )


@router.get('/nodes/realtime')
async def get_nodes_realtime(
    admin: User = Depends(require_permission('remnawave:read')),
) -> list[dict[str, Any]]:
    """Get realtime node usage data."""
    service = _get_service()
    _ensure_configured(service)

    return await service.get_nodes_realtime_usage()


@router.get('/nodes/{node_uuid}', response_model=NodeInfo)
async def get_node_details(
    node_uuid: str,
    admin: User = Depends(require_permission('remnawave:read')),
) -> NodeInfo:
    """Get detailed information about a specific node."""
    service = _get_service()
    _ensure_configured(service)

    node = await service.get_node_details(node_uuid)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Node not found',
        )

    return _serialize_node(node)


@router.get('/nodes/{node_uuid}/statistics', response_model=NodeStatisticsResponse)
async def get_node_statistics(
    node_uuid: str,
    admin: User = Depends(require_permission('remnawave:read')),
) -> NodeStatisticsResponse:
    """Get node statistics with usage history."""
    service = _get_service()
    _ensure_configured(service)

    stats = await service.get_node_statistics(node_uuid)
    if not stats or not stats.get('node'):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Node not found or no statistics available',
        )

    return NodeStatisticsResponse(
        node=_serialize_node(stats['node']),
        realtime=stats.get('realtime'),
        usage_history=stats.get('usage_history') or [],
        last_updated=_parse_datetime(stats.get('last_updated')),
    )


@router.get('/nodes/{node_uuid}/usage', response_model=NodeUsageResponse)
async def get_node_usage(
    node_uuid: str,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    admin: User = Depends(require_permission('remnawave:read')),
) -> NodeUsageResponse:
    """Get node usage history for a date range."""
    service = _get_service()
    _ensure_configured(service)

    end_dt = end or datetime.now(UTC)
    start_dt = start or (end_dt - timedelta(days=7))

    if start_dt >= end_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid date range',
        )

    usage = await service.get_node_user_usage_by_range(node_uuid, start_dt, end_dt)
    return NodeUsageResponse(items=usage or [])


@router.post('/nodes/{node_uuid}/action', response_model=NodeActionResponse)
async def perform_node_action(
    node_uuid: str,
    payload: NodeActionRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
) -> NodeActionResponse:
    """Perform an action on a node (enable/disable/restart)."""
    service = _get_service()
    _ensure_configured(service)

    # Get current node state for toggle operations
    if payload.action in ('enable', 'disable'):
        nodes = await service.get_all_nodes()
        node = next((n for n in nodes if n.get('uuid') == node_uuid), None)
        if not node:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Node not found',
            )

    success = await service.manage_node(node_uuid, payload.action)

    messages = {
        'enable': 'Node enabled',
        'disable': 'Node disabled',
        'restart': 'Node restart initiated',
    }

    if success:
        logger.info(
            'Admin performed on node', telegram_id=admin.telegram_id, action=payload.action, node_uuid=node_uuid
        )
        return NodeActionResponse(
            success=True,
            message=messages.get(payload.action, 'Action completed'),
            is_disabled=payload.action == 'disable' if payload.action in ('enable', 'disable') else None,
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f'Failed to {payload.action} node',
    )


@router.post('/nodes/restart-all', response_model=NodeActionResponse)
async def restart_all_nodes(
    admin: User = Depends(require_permission('remnawave:manage')),
) -> NodeActionResponse:
    """Restart all nodes."""
    service = _get_service()
    _ensure_configured(service)

    success = await service.restart_all_nodes()

    if success:
        logger.info('Admin restarted all nodes', telegram_id=admin.telegram_id)
        return NodeActionResponse(success=True, message='All nodes restart initiated')
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='Failed to restart all nodes',
    )


# ============ Squads (Internal Squads) ============


@router.get('/squads', response_model=SquadsListResponse)
async def list_squads(
    admin: User = Depends(require_permission('remnawave:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SquadsListResponse:
    """Get list of all squads with local database info."""
    service = _get_service()
    _ensure_configured(service)

    # Get squads from RemnaWave
    rw_squads = await service.get_all_squads()

    # Get local squads from DB
    local_squads, _ = await get_all_server_squads(db, page=1, limit=1000)
    local_by_uuid = {s.squad_uuid: s for s in local_squads}

    items = []
    for squad in rw_squads:
        local = local_by_uuid.get(squad.get('uuid'))
        items.append(
            SquadWithLocalInfo(
                uuid=squad.get('uuid', ''),
                name=squad.get('name', ''),
                members_count=squad.get('members_count', 0),
                inbounds_count=squad.get('inbounds_count', 0),
                inbounds=squad.get('inbounds', []),
                local_id=local.id if local else None,
                display_name=local.display_name if local else None,
                country_code=local.country_code if local else None,
                is_available=local.is_available if local else None,
                is_trial_eligible=local.is_trial_eligible if local else None,
                price_kopeks=local.price_kopeks if local else None,
                max_users=local.max_users if local else None,
                current_users=local.current_users if local else None,
                is_synced=local is not None,
            )
        )

    return SquadsListResponse(items=items, total=len(items))


@router.get('/squads/{squad_uuid}', response_model=SquadDetailResponse)
async def get_squad_details(
    squad_uuid: str,
    admin: User = Depends(require_permission('remnawave:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SquadDetailResponse:
    """Get detailed information about a squad."""
    service = _get_service()
    _ensure_configured(service)

    # Get squad from RemnaWave
    squad = await service.get_squad_details(squad_uuid)
    if not squad:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Squad not found',
        )

    # Get local info from DB
    local = await get_server_squad_by_uuid(db, squad_uuid)
    active_subs = await count_active_users_for_squad(db, squad_uuid) if local else 0

    return SquadDetailResponse(
        uuid=squad.get('uuid', ''),
        name=squad.get('name', ''),
        members_count=squad.get('members_count', 0),
        inbounds_count=squad.get('inbounds_count', 0),
        inbounds=squad.get('inbounds', []),
        local_id=local.id if local else None,
        display_name=local.display_name if local else None,
        country_code=local.country_code if local else None,
        description=local.description if local else None,
        is_available=local.is_available if local else None,
        is_trial_eligible=local.is_trial_eligible if local else None,
        price_kopeks=local.price_kopeks if local else None,
        max_users=local.max_users if local else None,
        current_users=local.current_users if local else None,
        sort_order=local.sort_order if local else None,
        is_synced=local is not None,
        active_subscriptions=active_subs,
    )


@router.post('/squads', response_model=SquadOperationResponse, status_code=status.HTTP_201_CREATED)
async def create_squad(
    payload: SquadCreateRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
) -> SquadOperationResponse:
    """Create a new squad in RemnaWave."""
    service = _get_service()
    _ensure_configured(service)

    squad_uuid = await service.create_squad(payload.name, payload.inbound_uuids)

    if squad_uuid:
        logger.info(
            'Admin created squad', telegram_id=admin.telegram_id, payload_name=payload.name, squad_uuid=squad_uuid
        )
        return SquadOperationResponse(
            success=True,
            message='Squad created successfully',
            data={'uuid': squad_uuid},
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='Failed to create squad',
    )


@router.patch('/squads/{squad_uuid}', response_model=SquadOperationResponse)
async def update_squad(
    squad_uuid: str,
    payload: SquadUpdateRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
) -> SquadOperationResponse:
    """Update a squad in RemnaWave."""
    service = _get_service()
    _ensure_configured(service)

    if payload.name is None and payload.inbound_uuids is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No update data provided',
        )

    success = await service.update_squad(
        squad_uuid,
        name=payload.name,
        inbounds=payload.inbound_uuids,
    )

    if success:
        logger.info('Admin updated squad', telegram_id=admin.telegram_id, squad_uuid=squad_uuid)
        return SquadOperationResponse(success=True, message='Squad updated')
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='Failed to update squad',
    )


@router.post('/squads/{squad_uuid}/action', response_model=SquadOperationResponse)
async def perform_squad_action(
    squad_uuid: str,
    payload: SquadActionRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
) -> SquadOperationResponse:
    """Perform an action on a squad."""
    service = _get_service()
    _ensure_configured(service)

    action = payload.action
    success = False
    message = 'Unknown action'

    if action == 'add_all_users':
        success = await service.add_all_users_to_squad(squad_uuid)
        message = 'Users added' if success else 'Failed to add users'
    elif action == 'remove_all_users':
        success = await service.remove_all_users_from_squad(squad_uuid)
        message = 'Users removed' if success else 'Failed to remove users'
    elif action == 'delete':
        success = await service.delete_squad(squad_uuid)
        message = 'Squad deleted' if success else 'Failed to delete squad'
    elif action == 'rename':
        if not payload.name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Name is required for rename action',
            )
        success = await service.rename_squad(squad_uuid, payload.name)
        message = 'Squad renamed' if success else 'Failed to rename squad'
    elif action == 'update_inbounds':
        if not payload.inbound_uuids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Inbound UUIDs are required',
            )
        success = await service.update_squad_inbounds(squad_uuid, payload.inbound_uuids)
        message = 'Inbounds updated' if success else 'Failed to update inbounds'

    if success:
        logger.info('Admin performed on squad', telegram_id=admin.telegram_id, action=action, squad_uuid=squad_uuid)

    return SquadOperationResponse(success=success, message=message)


@router.delete('/squads/{squad_uuid}', response_model=SquadOperationResponse)
async def delete_squad(
    squad_uuid: str,
    admin: User = Depends(require_permission('remnawave:manage')),
) -> SquadOperationResponse:
    """Delete a squad."""
    service = _get_service()
    _ensure_configured(service)

    success = await service.delete_squad(squad_uuid)

    if success:
        logger.info('Admin deleted squad', telegram_id=admin.telegram_id, squad_uuid=squad_uuid)
        return SquadOperationResponse(success=True, message='Squad deleted')
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='Failed to delete squad',
    )


# ============ Migration ============


@router.get('/squads/{squad_uuid}/migration-preview', response_model=MigrationPreviewResponse)
async def preview_migration(
    squad_uuid: str,
    admin: User = Depends(require_permission('remnawave:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MigrationPreviewResponse:
    """Get migration preview for a squad."""
    squad = await get_server_squad_by_uuid(db, squad_uuid)
    if not squad:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Squad not found in local database',
        )

    users_to_migrate = await count_active_users_for_squad(db, squad_uuid)

    return MigrationPreviewResponse(
        squad_uuid=squad.squad_uuid,
        squad_name=squad.display_name,
        current_users=squad.current_users or 0,
        max_users=squad.max_users,
        users_to_migrate=users_to_migrate,
    )


@router.post('/squads/migrate', response_model=MigrationResponse)
async def migrate_squad_users(
    payload: MigrationRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MigrationResponse:
    """Migrate users from one squad to another."""
    service = _get_service()
    _ensure_configured(service)

    source_uuid = payload.source_uuid.strip()
    target_uuid = payload.target_uuid.strip()

    if source_uuid == target_uuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Source and target squads must be different',
        )

    source = await get_server_squad_by_uuid(db, source_uuid)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Source squad not found',
        )

    target = await get_server_squad_by_uuid(db, target_uuid)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Target squad not found',
        )

    try:
        result = await service.migrate_squad_users(
            db,
            source_uuid=source.squad_uuid,
            target_uuid=target.squad_uuid,
        )
    except RemnaWaveConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    if not result.get('success'):
        return MigrationResponse(
            success=False,
            message=result.get('message') or 'Migration failed',
            error=result.get('error'),
        )

    logger.info(
        'Admin migrated users from to', telegram_id=admin.telegram_id, source_uuid=source_uuid, target_uuid=target_uuid
    )

    return MigrationResponse(
        success=True,
        message=result.get('message') or 'Migration completed',
        data=MigrationStats(
            source_uuid=source.squad_uuid,
            target_uuid=target.squad_uuid,
            total=result.get('total', 0),
            updated=result.get('updated', 0),
            panel_updated=result.get('panel_updated', 0),
            panel_failed=result.get('panel_failed', 0),
            source_removed=result.get('source_removed', 0),
            target_added=result.get('target_added', 0),
        ),
    )


# ============ Inbounds ============


@router.get('/inbounds', response_model=InboundsListResponse)
async def list_inbounds(
    admin: User = Depends(require_permission('remnawave:read')),
) -> InboundsListResponse:
    """Get list of all available inbounds."""
    service = _get_service()
    _ensure_configured(service)

    inbounds = await service.get_all_inbounds()
    return InboundsListResponse(items=inbounds or [], total=len(inbounds or []))


# ============ Auto Sync ============


@router.get('/sync/auto/status', response_model=AutoSyncStatus)
async def get_auto_sync_status(
    admin: User = Depends(require_permission('remnawave:read')),
) -> AutoSyncStatus:
    """Get auto sync status."""
    if remnawave_sync_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Auto sync service is not available',
        )

    status_obj = remnawave_sync_service.get_status()

    return AutoSyncStatus(
        enabled=status_obj.enabled,
        times=[t.strftime('%H:%M') for t in status_obj.times] if status_obj.times else [],
        next_run=status_obj.next_run,
        is_running=status_obj.is_running,
        last_run_started_at=status_obj.last_run_started_at,
        last_run_finished_at=status_obj.last_run_finished_at,
        last_run_success=status_obj.last_run_success,
        last_run_reason=status_obj.last_run_reason,
        last_run_error=status_obj.last_run_error,
        last_user_stats=status_obj.last_user_stats,
        last_server_stats=status_obj.last_server_stats,
    )


@router.post('/sync/auto/toggle', response_model=SyncResponse)
async def toggle_auto_sync(
    payload: AutoSyncToggleRequest,
    admin: User = Depends(require_permission('remnawave:sync')),
) -> SyncResponse:
    """Toggle auto sync on/off."""
    if remnawave_sync_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Auto sync service is not available',
        )

    # This would need to update settings - for now just return info
    # In production, this should update REMNAWAVE_AUTO_SYNC_ENABLED setting
    current_status = remnawave_sync_service.get_status()

    if payload.enabled and not current_status.enabled:
        # Enable - would need to update settings and refresh schedule
        remnawave_sync_service.schedule_refresh(run_immediately=True)
        logger.info('Admin enabled auto sync', telegram_id=admin.telegram_id)
        return SyncResponse(
            success=True,
            message='Auto sync enabled and scheduled',
        )
    if not payload.enabled and current_status.enabled:
        # Disable - would need to update settings and stop scheduler
        logger.info('Admin disabled auto sync', telegram_id=admin.telegram_id)
        return SyncResponse(
            success=True,
            message='Auto sync setting change requested. Restart may be required.',
        )
    return SyncResponse(
        success=True,
        message='No change needed',
    )


@router.post('/sync/auto/run', response_model=AutoSyncRunResponse)
async def run_auto_sync_now(
    admin: User = Depends(require_permission('remnawave:sync')),
) -> AutoSyncRunResponse:
    """Run auto sync immediately."""
    if remnawave_sync_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Auto sync service is not available',
        )

    logger.info('Admin triggered manual sync', telegram_id=admin.telegram_id)
    result = await remnawave_sync_service.run_sync_now(reason='manual')

    return AutoSyncRunResponse(
        started=result.get('started', False),
        success=result.get('success'),
        error=result.get('error'),
        user_stats=result.get('user_stats'),
        server_stats=result.get('server_stats'),
        reason='manual',
    )


# ============ Manual Sync ============


@router.post('/sync/from-panel', response_model=SyncResponse)
async def sync_from_panel(
    payload: SyncMode,
    admin: User = Depends(require_permission('remnawave:sync')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SyncResponse:
    """Sync users from RemnaWave panel to bot."""
    service = _get_service()
    _ensure_configured(service)

    try:
        stats = await service.sync_users_from_panel(db, payload.mode)
        logger.info('Admin synced from panel (mode: )', telegram_id=admin.telegram_id, mode=payload.mode)
        return SyncResponse(
            success=True,
            message='Sync from panel completed',
            data=stats,
        )
    except RemnaWaveConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )


@router.post('/sync/to-panel', response_model=SyncResponse)
async def sync_to_panel(
    admin: User = Depends(require_permission('remnawave:sync')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SyncResponse:
    """Sync users from bot to RemnaWave panel."""
    service = _get_service()
    _ensure_configured(service)

    stats = await service.sync_users_to_panel(db)
    logger.info('Admin synced to panel', telegram_id=admin.telegram_id)

    return SyncResponse(
        success=True,
        message='Sync to panel completed',
        data=stats,
    )


@router.post('/sync/servers', response_model=SyncResponse)
async def sync_servers(
    admin: User = Depends(require_permission('remnawave:sync')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SyncResponse:
    """Sync servers/squads from RemnaWave."""
    service = _get_service()
    _ensure_configured(service)

    squads = await service.get_all_squads()
    if not squads:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Failed to get squads from RemnaWave',
        )

    created, updated, removed = await sync_with_remnawave(db, squads)

    try:
        await cache.delete_pattern('available_countries*')
    except Exception as e:
        logger.warning('Failed to clear countries cache', error=e)

    logger.info(
        'Admin synced servers: created=, updated=, removed',
        telegram_id=admin.telegram_id,
        created=created,
        updated=updated,
        removed=removed,
    )

    return SyncResponse(
        success=True,
        message='Servers synced successfully',
        data={
            'created': created,
            'updated': updated,
            'removed': removed,
            'total': len(squads),
        },
    )


@router.post('/sync/subscriptions/validate', response_model=SyncResponse)
async def validate_subscriptions(
    admin: User = Depends(require_permission('remnawave:sync')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SyncResponse:
    """Validate and fix subscriptions."""
    service = _get_service()
    _ensure_configured(service)

    stats = await service.validate_and_fix_subscriptions(db)
    logger.info('Admin validated subscriptions', telegram_id=admin.telegram_id)

    return SyncResponse(
        success=True,
        message='Subscriptions validated',
        data=stats,
    )


@router.post('/sync/subscriptions/cleanup', response_model=SyncResponse)
async def cleanup_subscriptions(
    admin: User = Depends(require_permission('remnawave:sync')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SyncResponse:
    """Cleanup orphaned subscriptions."""
    service = _get_service()
    _ensure_configured(service)

    stats = await service.cleanup_orphaned_subscriptions(db)
    logger.info('Admin cleaned up subscriptions', telegram_id=admin.telegram_id)

    return SyncResponse(
        success=True,
        message='Cleanup completed',
        data=stats,
    )


@router.post('/sync/subscriptions/statuses', response_model=SyncResponse)
async def sync_subscription_statuses(
    admin: User = Depends(require_permission('remnawave:sync')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SyncResponse:
    """Sync subscription statuses."""
    service = _get_service()
    _ensure_configured(service)

    stats = await service.sync_subscription_statuses(db)
    logger.info('Admin synced subscription statuses', telegram_id=admin.telegram_id)

    return SyncResponse(
        success=True,
        message='Subscription statuses synced',
        data=stats,
    )


@router.get('/sync/recommendations', response_model=SyncResponse)
async def get_sync_recommendations(
    admin: User = Depends(require_permission('remnawave:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SyncResponse:
    """Get sync recommendations."""
    service = _get_service()
    _ensure_configured(service)

    data = await service.get_sync_recommendations(db)

    return SyncResponse(
        success=True,
        message='Recommendations retrieved',
        data=data,
    )
