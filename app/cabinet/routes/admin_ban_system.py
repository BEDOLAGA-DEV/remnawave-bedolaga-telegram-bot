"""Admin routes for Ban System monitoring in cabinet."""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config import settings
from app.database.models import User
from app.external.ban_system_api import BanSystemAPI, BanSystemAPIError

from ..dependencies import require_permission
from ..schemas.ban_system import (
    BanAgentHistoryItem,
    BanAgentHistoryResponse,
    BanAgentItem,
    BanAgentsListResponse,
    BanAgentsSummary,
    BanHealthComponent,
    BanHealthDetailedResponse,
    BanHealthResponse,
    BanHistoryResponse,
    BanNodeItem,
    BanNodesListResponse,
    BanPunishmentItem,
    BanPunishmentsListResponse,
    BanReportResponse,
    BanReportTopViolator,
    BanSettingDefinition,
    BanSettingsResponse,
    BanSystemStatsResponse,
    BanSystemStatusResponse,
    BanTrafficResponse,
    BanTrafficTopItem,
    BanTrafficViolationItem,
    BanTrafficViolationsResponse,
    BanUserDetailResponse,
    BanUserIPInfo,
    BanUserListItem,
    BanUserRequest,
    BanUserRequestLog,
    BanUsersListResponse,
    BanWhitelistRequest,
    UnbanResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/ban-system', tags=['Cabinet Admin Ban System'])


def _get_ban_api() -> BanSystemAPI:
    """Get Ban System API instance."""
    logger.debug(
        'Ban System check enabled: configured',
        is_ban_system_enabled=settings.is_ban_system_enabled(),
        is_ban_system_configured=settings.is_ban_system_configured(),
    )
    logger.debug('Ban System URL', get_ban_system_api_url=settings.get_ban_system_api_url())

    if not settings.is_ban_system_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Ban System integration is disabled',
        )

    if not settings.is_ban_system_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Ban System is not configured',
        )

    return BanSystemAPI(
        base_url=settings.get_ban_system_api_url(),
        api_token=settings.get_ban_system_api_token(),
        timeout=settings.get_ban_system_request_timeout(),
    )


async def _api_request(api: BanSystemAPI, method: str, *args, **kwargs) -> Any:
    """Execute API request with error handling."""
    try:
        async with api:
            func = getattr(api, method)
            return await func(*args, **kwargs)
    except BanSystemAPIError as e:
        logger.error('Ban System API error', error=e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'Ban System API error: {e.message}',
        )
    except Exception as e:
        logger.error('Ban System unexpected error', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Internal error: {e!s}',
        )


# === Status ===


@router.get('/status', response_model=BanSystemStatusResponse)
async def get_ban_system_status(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanSystemStatusResponse:
    """Get Ban System integration status."""
    return BanSystemStatusResponse(
        enabled=settings.is_ban_system_enabled(),
        configured=settings.is_ban_system_configured(),
    )


# === Stats ===


@router.get('/stats/raw')
async def get_stats_raw(
    admin: User = Depends(require_permission('ban_system:read')),
) -> dict:
    """Get raw stats from Ban System API for debugging."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_stats')
    return {'raw_response': data}


@router.get('/stats', response_model=BanSystemStatsResponse)
async def get_stats(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanSystemStatsResponse:
    """Get overall Ban System statistics."""
    from datetime import datetime

    api = _get_ban_api()
    data = await _api_request(api, 'get_stats')

    logger.debug('Ban System raw stats', data=data)

    # Extract punishment stats
    punishment_stats = data.get('punishment_stats') or {}

    # Extract connected nodes info
    connected_nodes = data.get('connected_nodes', [])

    # Count online nodes/agents
    nodes_online = sum(1 for n in connected_nodes if n.get('is_online', False))

    # Extract tcp_metrics for uptime
    tcp_metrics = data.get('tcp_metrics') or {}
    uptime_seconds = None
    intake_started = tcp_metrics.get('intake_started_at')
    if intake_started:
        try:
            start_time = datetime.fromisoformat(intake_started.replace('Z', '+00:00'))
            uptime_seconds = int((datetime.now(start_time.tzinfo) - start_time).total_seconds())
        except Exception:
            pass

    return BanSystemStatsResponse(
        total_users=data.get('total_users', 0),
        active_users=data.get('users_with_limit', 0),
        users_over_limit=data.get('users_over_limit', 0),
        total_requests=data.get('total_requests', 0),
        total_punishments=punishment_stats.get('total_punishments', 0),
        active_punishments=punishment_stats.get('active_punishments', 0),
        nodes_online=nodes_online,
        nodes_total=len(connected_nodes),
        agents_online=nodes_online,  # Agents = connected nodes with stats
        agents_total=len(connected_nodes),
        panel_connected=data.get('panel_loaded', False),
        uptime_seconds=uptime_seconds,
    )


# === Users ===


@router.get('/users', response_model=BanUsersListResponse)
async def get_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    status: str | None = Query(None, description='Filter: over_limit, with_limit, unlimited'),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanUsersListResponse:
    """Get list of users from Ban System."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_users', offset=offset, limit=limit, status=status)

    users = []
    for user_data in data.get('users', []):
        users.append(
            BanUserListItem(
                email=user_data.get('email', ''),
                unique_ip_count=user_data.get('unique_ip_count', 0),
                total_requests=user_data.get('total_requests', 0),
                limit=user_data.get('limit'),
                is_over_limit=user_data.get('is_over_limit', False),
                blocked_count=user_data.get('blocked_count', 0),
            )
        )

    return BanUsersListResponse(
        users=users,
        total=data.get('total', len(users)),
        offset=offset,
        limit=limit,
    )


@router.get('/users/over-limit', response_model=BanUsersListResponse)
async def get_users_over_limit(
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanUsersListResponse:
    """Get users who exceeded their device limit."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_users_over_limit', limit=limit)

    users = []
    for user_data in data.get('users', []):
        users.append(
            BanUserListItem(
                email=user_data.get('email', ''),
                unique_ip_count=user_data.get('unique_ip_count', 0),
                total_requests=user_data.get('total_requests', 0),
                limit=user_data.get('limit'),
                is_over_limit=True,
                blocked_count=user_data.get('blocked_count', 0),
            )
        )

    return BanUsersListResponse(
        users=users,
        total=len(users),
        offset=0,
        limit=limit,
    )


@router.get('/users/search/{query}')
async def search_users(
    query: str,
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanUsersListResponse:
    """Search for users."""
    api = _get_ban_api()
    data = await _api_request(api, 'search_users', query=query)

    users = []
    users_data = data.get('users', []) if isinstance(data, dict) else data
    for user_data in users_data:
        users.append(
            BanUserListItem(
                email=user_data.get('email', ''),
                unique_ip_count=user_data.get('unique_ip_count', 0),
                total_requests=user_data.get('total_requests', 0),
                limit=user_data.get('limit'),
                is_over_limit=user_data.get('is_over_limit', False),
                blocked_count=user_data.get('blocked_count', 0),
            )
        )

    return BanUsersListResponse(
        users=users,
        total=len(users),
        offset=0,
        limit=100,
    )


@router.get('/users/{email}', response_model=BanUserDetailResponse)
async def get_user_detail(
    email: str,
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanUserDetailResponse:
    """Get detailed user information."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_user', email=email)

    ips = []
    for ip_data in data.get('ips', {}).values() if isinstance(data.get('ips'), dict) else data.get('ips', []):
        ips.append(
            BanUserIPInfo(
                ip=ip_data.get('ip', ''),
                first_seen=ip_data.get('first_seen'),
                last_seen=ip_data.get('last_seen'),
                node=ip_data.get('node'),
                request_count=ip_data.get('request_count', 0),
                country_code=ip_data.get('country_code'),
                country_name=ip_data.get('country_name'),
                city=ip_data.get('city'),
            )
        )

    recent_requests = []
    for req_data in data.get('recent_requests', []):
        recent_requests.append(
            BanUserRequestLog(
                timestamp=req_data.get('timestamp'),
                source_ip=req_data.get('source_ip', ''),
                destination=req_data.get('destination'),
                dest_port=req_data.get('dest_port'),
                protocol=req_data.get('protocol'),
                action=req_data.get('action'),
                node=req_data.get('node'),
            )
        )

    return BanUserDetailResponse(
        email=data.get('email', email),
        unique_ip_count=data.get('unique_ip_count', 0),
        total_requests=data.get('total_requests', 0),
        limit=data.get('limit'),
        is_over_limit=data.get('is_over_limit', False),
        blocked_count=data.get('blocked_count', 0),
        ips=ips,
        recent_requests=recent_requests,
        network_type=data.get('network_type'),
    )


# === Punishments ===


@router.get('/punishments', response_model=BanPunishmentsListResponse)
async def get_punishments(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanPunishmentsListResponse:
    """Get list of active punishments (bans)."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_punishments')

    punishments = []
    punishments_data = data if isinstance(data, list) else data.get('punishments', [])
    for p in punishments_data:
        punishments.append(
            BanPunishmentItem(
                id=p.get('id'),
                user_id=p.get('user_id', ''),
                uuid=p.get('uuid'),
                username=p.get('username', ''),
                reason=p.get('reason'),
                punished_at=p.get('punished_at'),
                enable_at=p.get('enable_at'),
                ip_count=p.get('ip_count', 0),
                limit=p.get('limit', 0),
                enabled=p.get('enabled', False),
                enabled_at=p.get('enabled_at'),
                node_name=p.get('node_name'),
            )
        )

    return BanPunishmentsListResponse(
        punishments=punishments,
        total=len(punishments),
    )


@router.post('/punishments/{user_id}/unban', response_model=UnbanResponse)
async def unban_user(
    user_id: str,
    admin: User = Depends(require_permission('ban_system:unban')),
) -> UnbanResponse:
    """Unban (enable) a user."""
    api = _get_ban_api()
    try:
        await _api_request(api, 'enable_user', user_id=user_id)
        logger.info('Admin unbanned user in Ban System', admin_id=admin.id, user_id=user_id)
        return UnbanResponse(success=True, message='User unbanned successfully')
    except HTTPException:
        raise
    except Exception as e:
        return UnbanResponse(success=False, message=str(e))


@router.post('/ban', response_model=UnbanResponse)
async def ban_user(
    request: BanUserRequest,
    admin: User = Depends(require_permission('ban_system:ban')),
) -> UnbanResponse:
    """Manually ban a user."""
    api = _get_ban_api()
    try:
        await _api_request(
            api,
            'ban_user',
            username=request.username,
            minutes=request.minutes,
            reason=request.reason,
        )
        logger.info('Admin banned user', admin_id=admin.id, username=request.username, reason=request.reason)
        return UnbanResponse(success=True, message='User banned successfully')
    except HTTPException:
        raise
    except Exception as e:
        return UnbanResponse(success=False, message=str(e))


@router.get('/history/{query}', response_model=BanHistoryResponse)
async def get_punishment_history(
    query: str,
    limit: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanHistoryResponse:
    """Get punishment history for a user."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_punishment_history', query=query, limit=limit)

    items = []
    history_data = data if isinstance(data, list) else data.get('items', [])
    for p in history_data:
        items.append(
            BanPunishmentItem(
                id=p.get('id'),
                user_id=p.get('user_id', ''),
                uuid=p.get('uuid'),
                username=p.get('username', ''),
                reason=p.get('reason'),
                punished_at=p.get('punished_at'),
                enable_at=p.get('enable_at'),
                ip_count=p.get('ip_count', 0),
                limit=p.get('limit', 0),
                enabled=p.get('enabled', False),
                enabled_at=p.get('enabled_at'),
                node_name=p.get('node_name'),
            )
        )

    return BanHistoryResponse(
        items=items,
        total=len(items),
    )


# === Nodes ===


@router.get('/nodes', response_model=BanNodesListResponse)
async def get_nodes(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanNodesListResponse:
    """Get list of connected nodes."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_nodes')

    nodes = []
    nodes_data = data if isinstance(data, list) else data.get('nodes', [])
    online_count = 0
    for n in nodes_data:
        # API returns is_online, not is_connected
        is_connected = n.get('is_online', n.get('is_connected', False))
        if is_connected:
            online_count += 1
        nodes.append(
            BanNodeItem(
                name=n.get('name', ''),
                address=n.get('address'),
                is_connected=is_connected,
                # API returns last_heartbeat, not last_seen
                last_seen=n.get('last_heartbeat', n.get('last_seen')),
                # API returns unique_users, not users_count
                users_count=n.get('unique_users', n.get('users_count', 0)),
                agent_stats=n.get('agent_stats'),
            )
        )

    return BanNodesListResponse(
        nodes=nodes,
        total=len(nodes),
        online=online_count,
    )


# === Agents ===


@router.get('/agents', response_model=BanAgentsListResponse)
async def get_agents(
    search: str | None = Query(None),
    health: str | None = Query(None, description='healthy, warning, critical'),
    agent_status: str | None = Query(None, alias='status', description='online, offline'),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanAgentsListResponse:
    """Get list of monitoring agents."""
    api = _get_ban_api()
    data = await _api_request(
        api,
        'get_agents',
        search=search,
        health=health,
        status=agent_status,
    )

    agents = []
    agents_data = data.get('agents', {}) if isinstance(data, dict) else data
    online_count = 0

    # API returns agents as dict: {"node_name": {stats...}, ...}
    if isinstance(agents_data, dict):
        for node_name, agent_info in agents_data.items():
            # Extract metrics from nested structure
            stats = agent_info.get('stats', {}) or {}
            metrics = stats.get('metrics', {}) or {}
            sent_info = metrics.get('sent', {}) or {}
            queue_info = metrics.get('queue', {}) or {}
            conn_info = metrics.get('connection', {}) or {}

            is_online = agent_info.get('is_online', False)
            if is_online:
                online_count += 1

            agents.append(
                BanAgentItem(
                    node_name=node_name,
                    sent_total=sent_info.get('total', 0),
                    dropped_total=sent_info.get('dropped', 0),
                    batches_total=sent_info.get('batches', 0),
                    reconnects=conn_info.get('reconnects', 0),
                    failures=conn_info.get('failures', sent_info.get('failed', 0)),
                    queue_size=queue_info.get('current', 0),
                    queue_max=queue_info.get('high_watermark', 0),
                    dedup_checked=0,
                    dedup_skipped=0,
                    filter_checked=0,
                    filter_filtered=0,
                    health=agent_info.get('health', 'unknown'),
                    is_online=is_online,
                    last_report=agent_info.get('updated_at'),
                )
            )
    else:
        # Fallback for list format
        for a in agents_data:
            is_online = a.get('is_online', False)
            if is_online:
                online_count += 1
            agents.append(
                BanAgentItem(
                    node_name=a.get('node_name', ''),
                    sent_total=a.get('sent_total', 0),
                    dropped_total=a.get('dropped_total', 0),
                    batches_total=a.get('batches_total', 0),
                    reconnects=a.get('reconnects', 0),
                    failures=a.get('failures', 0),
                    queue_size=a.get('queue_size', 0),
                    queue_max=a.get('queue_max', 0),
                    dedup_checked=a.get('dedup_checked', 0),
                    dedup_skipped=a.get('dedup_skipped', 0),
                    filter_checked=a.get('filter_checked', 0),
                    filter_filtered=a.get('filter_filtered', 0),
                    health=a.get('health', 'unknown'),
                    is_online=is_online,
                    last_report=a.get('last_report'),
                )
            )

    summary = None
    if isinstance(data, dict) and 'summary' in data:
        s = data['summary']
        summary = BanAgentsSummary(
            total_agents=s.get('total_agents', len(agents)),
            online_agents=s.get('online_agents', online_count),
            total_sent=s.get('total_sent', 0),
            total_dropped=s.get('total_dropped', 0),
            avg_queue_size=s.get('avg_queue_size', 0.0),
            healthy_count=s.get('healthy_count', 0),
            warning_count=s.get('warning_count', 0),
            critical_count=s.get('critical_count', 0),
        )

    return BanAgentsListResponse(
        agents=agents,
        summary=summary,
        total=len(agents),
        online=online_count,
    )


@router.get('/agents/summary', response_model=BanAgentsSummary)
async def get_agents_summary(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanAgentsSummary:
    """Get agents summary statistics."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_agents_summary')

    return BanAgentsSummary(
        total_agents=data.get('total_agents', 0),
        online_agents=data.get('online_agents', 0),
        total_sent=data.get('total_sent', 0),
        total_dropped=data.get('total_dropped', 0),
        avg_queue_size=data.get('avg_queue_size', 0.0),
        healthy_count=data.get('healthy_count', 0),
        warning_count=data.get('warning_count', 0),
        critical_count=data.get('critical_count', 0),
    )


# === Traffic Violations ===


@router.get('/traffic/violations', response_model=BanTrafficViolationsResponse)
async def get_traffic_violations(
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanTrafficViolationsResponse:
    """Get list of traffic limit violations."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_traffic_violations', limit=limit)

    violations = []
    violations_data = data if isinstance(data, list) else data.get('violations', [])
    for v in violations_data:
        violations.append(
            BanTrafficViolationItem(
                id=v.get('id'),
                username=v.get('username', ''),
                email=v.get('email'),
                violation_type=v.get('violation_type', v.get('type', '')),
                description=v.get('description'),
                bytes_used=v.get('bytes_used', 0),
                bytes_limit=v.get('bytes_limit', 0),
                detected_at=v.get('detected_at'),
                resolved=v.get('resolved', False),
            )
        )

    return BanTrafficViolationsResponse(
        violations=violations,
        total=len(violations),
    )


# === Full Traffic Stats ===


@router.get('/traffic', response_model=BanTrafficResponse)
async def get_traffic(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanTrafficResponse:
    """Get full traffic statistics including top users."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_traffic')

    top_users = []
    for u in data.get('top_users', []):
        top_users.append(
            BanTrafficTopItem(
                username=u.get('username', ''),
                bytes_total=u.get('bytes_total', u.get('total_bytes', 0)),
                bytes_limit=u.get('bytes_limit'),
                over_limit=u.get('over_limit', False),
            )
        )

    violations = []
    for v in data.get('recent_violations', []):
        violations.append(
            BanTrafficViolationItem(
                id=v.get('id'),
                username=v.get('username', ''),
                email=v.get('email'),
                violation_type=v.get('violation_type', v.get('type', '')),
                description=v.get('description'),
                bytes_used=v.get('bytes_used', 0),
                bytes_limit=v.get('bytes_limit', 0),
                detected_at=v.get('detected_at'),
                resolved=v.get('resolved', False),
            )
        )

    return BanTrafficResponse(
        enabled=data.get('enabled', False),
        stats=data.get('stats'),
        top_users=top_users,
        recent_violations=violations,
    )


@router.get('/traffic/top')
async def get_traffic_top(
    limit: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_permission('ban_system:read')),
) -> list[BanTrafficTopItem]:
    """Get top users by traffic."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_traffic_top', limit=limit)

    top_users = []
    users_data = data if isinstance(data, list) else data.get('users', [])
    for u in users_data:
        top_users.append(
            BanTrafficTopItem(
                username=u.get('username', ''),
                bytes_total=u.get('bytes_total', u.get('total_bytes', 0)),
                bytes_limit=u.get('bytes_limit'),
                over_limit=u.get('over_limit', False),
            )
        )

    return top_users


# === Settings ===


def _parse_setting_response(key: str, data: Any, default_type: str = 'str') -> BanSettingDefinition:
    """Parse setting response from API."""
    if isinstance(data, dict) and 'value' in data:
        return BanSettingDefinition(
            key=key,
            value=data.get('value'),
            type=data.get('type', default_type),
            min_value=data.get('min'),
            max_value=data.get('max'),
            editable=data.get('editable', True),
            description=data.get('description'),
            category=data.get('category'),
        )
    # Простое значение или dict без "value"
    value = data.get('value', data) if isinstance(data, dict) else data
    value_type = default_type
    if isinstance(value, bool):
        value_type = 'bool'
    elif isinstance(value, int):
        value_type = 'int'
    elif isinstance(value, float):
        value_type = 'float'
    elif isinstance(value, list):
        value_type = 'list'

    return BanSettingDefinition(
        key=key,
        value=value,
        type=value_type,
        min_value=None,
        max_value=None,
        editable=True,
        description=None,
        category=None,
    )


@router.get('/settings', response_model=BanSettingsResponse)
async def get_settings(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanSettingsResponse:
    """Get all Ban System settings."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_settings')

    settings_list = []
    settings_data = data.get('settings', {}) if isinstance(data, dict) else {}

    for key, info in settings_data.items():
        # API может возвращать настройки в двух форматах:
        # 1. {"key": {"value": ..., "type": ...}} - с метаданными
        # 2. {"key": value} - просто значение
        if isinstance(info, dict) and 'value' in info:
            # Формат с метаданными
            settings_list.append(
                BanSettingDefinition(
                    key=key,
                    value=info.get('value'),
                    type=info.get('type', 'str'),
                    min_value=info.get('min'),
                    max_value=info.get('max'),
                    editable=info.get('editable', True),
                    description=info.get('description'),
                    category=info.get('category'),
                )
            )
        else:
            # Простой формат - определяем тип по значению
            value_type = 'str'
            if isinstance(info, bool):
                value_type = 'bool'
            elif isinstance(info, int):
                value_type = 'int'
            elif isinstance(info, float):
                value_type = 'float'
            elif isinstance(info, list):
                value_type = 'list'

            settings_list.append(
                BanSettingDefinition(
                    key=key,
                    value=info,
                    type=value_type,
                    min_value=None,
                    max_value=None,
                    editable=True,
                    description=None,
                    category=None,
                )
            )

    return BanSettingsResponse(settings=settings_list)


@router.get('/settings/{key}')
async def get_setting(
    key: str,
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanSettingDefinition:
    """Get a specific setting."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_setting', key=key)

    return _parse_setting_response(key, data)


@router.post('/settings/{key}')
async def set_setting(
    key: str,
    value: str = Query(...),
    admin: User = Depends(require_permission('ban_system:edit')),
) -> BanSettingDefinition:
    """Set a setting value."""
    api = _get_ban_api()
    data = await _api_request(api, 'set_setting', key=key, value=value)

    logger.info('Admin changed Ban System setting to', admin_id=admin.id, key=key, value=value)

    return _parse_setting_response(key, data)


@router.post('/settings/{key}/toggle')
async def toggle_setting(
    key: str,
    admin: User = Depends(require_permission('ban_system:edit')),
) -> BanSettingDefinition:
    """Toggle a boolean setting."""
    api = _get_ban_api()
    data = await _api_request(api, 'toggle_setting', key=key)

    logger.info('Admin toggled Ban System setting', admin_id=admin.id, key=key)

    return _parse_setting_response(key, data, default_type='bool')


# === Whitelist ===


@router.post('/settings/whitelist/add', response_model=UnbanResponse)
async def whitelist_add(
    request: BanWhitelistRequest,
    admin: User = Depends(require_permission('ban_system:edit')),
) -> UnbanResponse:
    """Add user to whitelist."""
    api = _get_ban_api()
    try:
        await _api_request(api, 'whitelist_add', username=request.username)
        logger.info('Admin added to Ban System whitelist', admin_id=admin.id, username=request.username)
        return UnbanResponse(success=True, message=f'User {request.username} added to whitelist')
    except HTTPException:
        raise
    except Exception as e:
        return UnbanResponse(success=False, message=str(e))


@router.post('/settings/whitelist/remove', response_model=UnbanResponse)
async def whitelist_remove(
    request: BanWhitelistRequest,
    admin: User = Depends(require_permission('ban_system:edit')),
) -> UnbanResponse:
    """Remove user from whitelist."""
    api = _get_ban_api()
    try:
        await _api_request(api, 'whitelist_remove', username=request.username)
        logger.info('Admin removed from Ban System whitelist', admin_id=admin.id, username=request.username)
        return UnbanResponse(success=True, message=f'User {request.username} removed from whitelist')
    except HTTPException:
        raise
    except Exception as e:
        return UnbanResponse(success=False, message=str(e))


# === Reports ===


@router.get('/report', response_model=BanReportResponse)
async def get_report(
    hours: int = Query(24, ge=1, le=168),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanReportResponse:
    """Get period report."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_stats_period', hours=hours)

    top_violators = []
    punishment_stats = data.get('punishment_stats', {}) or {}
    for v in punishment_stats.get('top_violators', []):
        top_violators.append(
            BanReportTopViolator(
                username=v.get('username', ''),
                count=v.get('count', 0),
            )
        )

    return BanReportResponse(
        period_hours=hours,
        current_users=data.get('current_users', 0),
        current_ips=data.get('current_ips', 0),
        punishment_stats=punishment_stats,
        top_violators=top_violators,
    )


# === Health ===


@router.get('/health', response_model=BanHealthResponse)
async def get_health(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanHealthResponse:
    """Get Ban System health status."""
    api = _get_ban_api()
    data = await _api_request(api, 'health_check')

    components = []
    for name, info in data.get('components', {}).items():
        if isinstance(info, dict):
            components.append(
                BanHealthComponent(
                    name=name,
                    status=info.get('status', 'unknown'),
                    message=info.get('message'),
                    details=info.get('details'),
                )
            )
        else:
            components.append(
                BanHealthComponent(
                    name=name,
                    status=str(info) if info else 'unknown',
                )
            )

    return BanHealthResponse(
        status=data.get('status', 'unknown'),
        uptime=data.get('uptime'),
        components=components,
    )


@router.get('/health/detailed', response_model=BanHealthDetailedResponse)
async def get_health_detailed(
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanHealthDetailedResponse:
    """Get detailed health information."""
    api = _get_ban_api()
    data = await _api_request(api, 'health_detailed')

    return BanHealthDetailedResponse(
        status=data.get('status', 'unknown'),
        uptime=data.get('uptime'),
        components=data.get('components', {}),
    )


# === Agent History ===


@router.get('/agents/{node_name}/history', response_model=BanAgentHistoryResponse)
async def get_agent_history(
    node_name: str,
    hours: int = Query(24, ge=1, le=168),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanAgentHistoryResponse:
    """Get agent statistics history."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_agent_history', node_name=node_name, hours=hours)

    history = []
    for item in data.get('history', []):
        history.append(
            BanAgentHistoryItem(
                timestamp=item.get('timestamp'),
                sent_total=item.get('sent_total', 0),
                dropped_total=item.get('dropped_total', 0),
                queue_size=item.get('queue_size', 0),
                batches_total=item.get('batches_total', 0),
            )
        )

    return BanAgentHistoryResponse(
        node=data.get('node', node_name),
        hours=data.get('hours', hours),
        records=data.get('records', len(history)),
        delta=data.get('delta'),
        first=data.get('first'),
        last=data.get('last'),
        history=history,
    )


# === User Punishment History ===


@router.get('/users/{email}/history', response_model=BanHistoryResponse)
async def get_user_punishment_history(
    email: str,
    limit: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_permission('ban_system:read')),
) -> BanHistoryResponse:
    """Get punishment history for a specific user."""
    api = _get_ban_api()
    data = await _api_request(api, 'get_punishment_history', query=email, limit=limit)

    items = []
    history_data = data if isinstance(data, list) else data.get('items', [])
    for p in history_data:
        items.append(
            BanPunishmentItem(
                id=p.get('id'),
                user_id=p.get('user_id', ''),
                uuid=p.get('uuid'),
                username=p.get('username', ''),
                reason=p.get('reason'),
                punished_at=p.get('punished_at'),
                enable_at=p.get('enable_at'),
                ip_count=p.get('ip_count', 0),
                limit=p.get('limit', 0),
                enabled=p.get('enabled', False),
                enabled_at=p.get('enabled_at'),
                node_name=p.get('node_name'),
            )
        )

    return BanHistoryResponse(
        items=items,
        total=len(items),
    )
