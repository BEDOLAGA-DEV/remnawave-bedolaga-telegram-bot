from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import (
    count_active_users_for_squad,
    get_server_squad_by_uuid,
)
from app.localization.texts import get_texts

from ..dependencies import get_db_session, require_api_token
from ..schemas.remnawave import (
    RemnaWaveConnectionStatus,
    RemnaWaveGenericSyncResponse,
    RemnaWaveInboundsResponse,
    RemnaWaveNode,
    RemnaWaveNodeActionRequest,
    RemnaWaveNodeActionResponse,
    RemnaWaveNodeListResponse,
    RemnaWaveNodeStatisticsResponse,
    RemnaWaveNodeUsageResponse,
    RemnaWaveOperationResponse,
    RemnaWaveSquad,
    RemnaWaveSquadActionRequest,
    RemnaWaveSquadCreateRequest,
    RemnaWaveSquadListResponse,
    RemnaWaveSquadMigrationPreviewResponse,
    RemnaWaveSquadMigrationRequest,
    RemnaWaveSquadMigrationResponse,
    RemnaWaveSquadMigrationStats,
    RemnaWaveSquadUpdateRequest,
    RemnaWaveStatusResponse,
    RemnaWaveSyncFromPanelRequest,
    RemnaWaveSystemStatsResponse,
    RemnaWaveUserTrafficResponse,
)


try:  # pragma: no cover - импорт может не работать без optional-зависимостей
    from app.services.remnawave_service import (  # type: ignore
        RemnaWaveConfigurationError,
        RemnaWaveService,
    )
except Exception:  # pragma: no cover - при ошибке импорта скрываем функционал
    RemnaWaveConfigurationError = None  # type: ignore[assignment]
    RemnaWaveService = None  # type: ignore[assignment]

if TYPE_CHECKING:  # pragma: no cover - только для типов в IDE
    from app.services.remnawave_service import RemnaWaveService as RemnaWaveServiceType
else:
    RemnaWaveServiceType = Any


router = APIRouter()



def _get_service() -> RemnaWaveServiceType:
    texts = get_texts('ru')
    if RemnaWaveService is None:  # pragma: no cover - зависимость не доступна
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=texts.t(
                'WEBAPI_REMNAWAVE_SERVICE_UNAVAILABLE',
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
                'WEBAPI_REMNAWAVE_SERVICE_UNAVAILABLE',
                'RemnaWave сервис недоступен',
            ),
        )

    if not service.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=service.configuration_error
            or texts.t(
                'WEBAPI_REMNAWAVE_API_NOT_CONFIGURED',
                'RemnaWave API не настроен',
            ),
        )


def _serialize_node(node_data: dict[str, Any]) -> RemnaWaveNode:
    return RemnaWaveNode(
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
        last_status_change=_parse_last_updated(node_data.get('last_status_change')),
        last_status_message=node_data.get('last_status_message'),
        xray_uptime=node_data.get('xray_uptime'),
        is_traffic_tracking_active=bool(node_data.get('is_traffic_tracking_active', False)),
        traffic_reset_day=node_data.get('traffic_reset_day'),
        notify_percent=node_data.get('notify_percent'),
        consumption_multiplier=float(node_data.get('consumption_multiplier', 1.0)),
        cpu_count=node_data.get('cpu_count'),
        cpu_model=node_data.get('cpu_model'),
        total_ram=node_data.get('total_ram'),
        created_at=_parse_last_updated(node_data.get('created_at')),
        updated_at=_parse_last_updated(node_data.get('updated_at')),
        provider_uuid=node_data.get('provider_uuid'),
    )


def _parse_last_updated(value: Any) -> datetime | None:
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


@router.get('/status', response_model=RemnaWaveStatusResponse)
async def get_remnawave_status(
    _: Any = Security(require_api_token),
) -> RemnaWaveStatusResponse:
    service = _get_service()

    connection_info: RemnaWaveConnectionStatus | None = None
    connection_result = await service.test_api_connection()

    if connection_result:
        connection_info = RemnaWaveConnectionStatus(**connection_result)

    return RemnaWaveStatusResponse(
        is_configured=service.is_configured,
        configuration_error=service.configuration_error,
        connection=connection_info,
    )


@router.get('/system', response_model=RemnaWaveSystemStatsResponse)
async def get_system_statistics(
    _: Any = Security(require_api_token),
) -> RemnaWaveSystemStatsResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    stats = await service.get_system_statistics()
    if not stats or 'system' not in stats:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            texts.t(
                'WEBAPI_REMNAWAVE_SYSTEM_STATS_UNAVAILABLE',
                'Не удалось получить статистику RemnaWave',
            ),
        )

    stats['last_updated'] = _parse_last_updated(stats.get('last_updated'))
    return RemnaWaveSystemStatsResponse(**stats)


@router.get('/nodes', response_model=RemnaWaveNodeListResponse)
async def list_nodes(
    _: Any = Security(require_api_token),
) -> RemnaWaveNodeListResponse:
    service = _get_service()
    _ensure_service_configured(service)

    nodes = await service.get_all_nodes()
    serialized = [_serialize_node(node) for node in nodes]
    return RemnaWaveNodeListResponse(items=serialized, total=len(serialized))


@router.get('/nodes/realtime', response_model=list[dict[str, Any]])
async def get_nodes_realtime_usage(
    _: Any = Security(require_api_token),
) -> list[dict[str, Any]]:
    service = _get_service()
    _ensure_service_configured(service)
    return await service.get_nodes_realtime_usage()


@router.get('/nodes/{node_uuid}', response_model=RemnaWaveNode)
async def get_node_details(
    node_uuid: str,
    _: Any = Security(require_api_token),
) -> RemnaWaveNode:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    node = await service.get_node_details(node_uuid)
    if not node:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_REMNAWAVE_NODE_NOT_FOUND', 'Нода не найдена'),
        )
    return _serialize_node(node)


@router.get('/nodes/{node_uuid}/statistics', response_model=RemnaWaveNodeStatisticsResponse)
async def get_node_statistics(
    node_uuid: str,
    _: Any = Security(require_api_token),
) -> RemnaWaveNodeStatisticsResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    stats = await service.get_node_statistics(node_uuid)
    if not stats or not stats.get('node'):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t(
                'WEBAPI_REMNAWAVE_NODE_INFO_NOT_FOUND',
                'Не удалось получить информацию по ноде',
            ),
        )

    node_data = _serialize_node(stats['node'])
    usage_history = stats.get('usage_history') or []
    realtime = stats.get('realtime')
    last_updated = _parse_last_updated(stats.get('last_updated'))

    return RemnaWaveNodeStatisticsResponse(
        node=node_data,
        realtime=realtime,
        usage_history=usage_history,
        last_updated=last_updated,
    )


@router.get('/nodes/{node_uuid}/usage', response_model=RemnaWaveNodeUsageResponse)
async def get_node_usage_range(
    node_uuid: str,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    _: Any = Security(require_api_token),
) -> RemnaWaveNodeUsageResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    end_dt = end or datetime.now(UTC)
    start_dt = start or (end_dt - timedelta(days=7))

    if start_dt >= end_dt:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_REMNAWAVE_INVALID_DATE_RANGE', 'Некорректный диапазон дат'),
        )

    usage = await service.get_node_user_usage_by_range(node_uuid, start_dt, end_dt)
    return RemnaWaveNodeUsageResponse(items=usage or [])


@router.post('/nodes/{node_uuid}/actions', response_model=RemnaWaveNodeActionResponse)
async def manage_node(
    node_uuid: str,
    payload: RemnaWaveNodeActionRequest,
    _: Any = Security(require_api_token),
) -> RemnaWaveNodeActionResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    success = await service.manage_node(node_uuid, payload.action)
    detail = None
    if success:
        if payload.action == 'enable':
            detail = texts.t('WEBAPI_REMNAWAVE_NODE_ACTION_ENABLED', 'Нода включена')
        elif payload.action == 'disable':
            detail = texts.t('WEBAPI_REMNAWAVE_NODE_ACTION_DISABLED', 'Нода отключена')
        elif payload.action == 'restart':
            detail = texts.t('WEBAPI_REMNAWAVE_NODE_ACTION_RESTART_SENT', 'Команда перезапуска отправлена')
    else:
        detail = texts.t('WEBAPI_REMNAWAVE_NODE_ACTION_FAILED', 'Не удалось выполнить действие')

    return RemnaWaveNodeActionResponse(success=success, detail=detail)


@router.post('/nodes/restart', response_model=RemnaWaveNodeActionResponse)
async def restart_all_nodes(
    _: Any = Security(require_api_token),
) -> RemnaWaveNodeActionResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    success = await service.restart_all_nodes()
    detail = (
        texts.t('WEBAPI_REMNAWAVE_RESTART_ALL_SENT', 'Команда перезапуска отправлена')
        if success
        else texts.t('WEBAPI_REMNAWAVE_RESTART_ALL_FAILED', 'Не удалось перезапустить ноды')
    )
    return RemnaWaveNodeActionResponse(success=success, detail=detail)


@router.get('/squads', response_model=RemnaWaveSquadListResponse)
async def list_squads(
    _: Any = Security(require_api_token),
) -> RemnaWaveSquadListResponse:
    service = _get_service()
    _ensure_service_configured(service)

    squads = await service.get_all_squads()
    serialized = [RemnaWaveSquad(**squad) for squad in squads]
    return RemnaWaveSquadListResponse(items=serialized, total=len(serialized))


@router.get('/squads/{squad_uuid}', response_model=RemnaWaveSquad)
async def get_squad_details(
    squad_uuid: str,
    _: Any = Security(require_api_token),
) -> RemnaWaveSquad:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    squad = await service.get_squad_details(squad_uuid)
    if not squad:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_REMNAWAVE_SQUAD_NOT_FOUND', 'Сквад не найден'),
        )
    return RemnaWaveSquad(**squad)


@router.post('/squads', response_model=RemnaWaveOperationResponse, status_code=status.HTTP_201_CREATED)
async def create_squad(
    payload: RemnaWaveSquadCreateRequest,
    _: Any = Security(require_api_token),
) -> RemnaWaveOperationResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    squad_uuid = await service.create_squad(payload.name, payload.inbound_uuids)

    success = squad_uuid is not None
    detail = (
        texts.t('WEBAPI_REMNAWAVE_SQUAD_CREATE_SUCCESS', 'Сквад успешно создан')
        if success
        else texts.t('WEBAPI_REMNAWAVE_SQUAD_CREATE_FAILED', 'Не удалось создать сквад')
    )
    data = {'uuid': squad_uuid} if success else None

    return RemnaWaveOperationResponse(success=success, detail=detail, data=data)


@router.patch('/squads/{squad_uuid}', response_model=RemnaWaveOperationResponse)
async def update_squad(
    squad_uuid: str,
    payload: RemnaWaveSquadUpdateRequest,
    _: Any = Security(require_api_token),
) -> RemnaWaveOperationResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    success = False
    detail = texts.t('WEBAPI_REMNAWAVE_SQUAD_UPDATE_DATA_REQUIRED', 'Необходимо указать новые данные')

    if payload.name is not None or payload.inbound_uuids is not None:
        success = await service.update_squad(
            squad_uuid,
            name=payload.name,
            inbounds=payload.inbound_uuids,
        )
        detail = (
            texts.t('WEBAPI_REMNAWAVE_SQUAD_UPDATE_SUCCESS', 'Сквад обновлен')
            if success
            else texts.t('WEBAPI_REMNAWAVE_SQUAD_UPDATE_FAILED', 'Не удалось обновить сквад')
        )

    return RemnaWaveOperationResponse(success=success, detail=detail)


@router.post('/squads/{squad_uuid}/actions', response_model=RemnaWaveOperationResponse)
async def squad_actions(
    squad_uuid: str,
    payload: RemnaWaveSquadActionRequest,
    _: Any = Security(require_api_token),
) -> RemnaWaveOperationResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    action = payload.action
    success = False
    detail = texts.t('WEBAPI_REMNAWAVE_UNKNOWN_ACTION', 'Неизвестное действие')

    if action == 'add_all_users':
        success = await service.add_all_users_to_squad(squad_uuid)
        detail = (
            texts.t('WEBAPI_REMNAWAVE_USERS_ADD_SUCCESS', 'Пользователи добавлены')
            if success
            else texts.t('WEBAPI_REMNAWAVE_USERS_ADD_FAILED', 'Не удалось добавить пользователей')
        )
    elif action == 'remove_all_users':
        success = await service.remove_all_users_from_squad(squad_uuid)
        detail = (
            texts.t('WEBAPI_REMNAWAVE_USERS_REMOVE_SUCCESS', 'Пользователи удалены')
            if success
            else texts.t('WEBAPI_REMNAWAVE_USERS_REMOVE_FAILED', 'Не удалось удалить пользователей')
        )
    elif action == 'delete':
        success = await service.delete_squad(squad_uuid)
        detail = (
            texts.t('WEBAPI_REMNAWAVE_SQUAD_DELETE_SUCCESS', 'Сквад удален')
            if success
            else texts.t('WEBAPI_REMNAWAVE_SQUAD_DELETE_FAILED', 'Не удалось удалить сквад')
        )
    elif action == 'rename':
        if not payload.name:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t('WEBAPI_REMNAWAVE_SQUAD_RENAME_REQUIRED', 'Необходимо указать новое имя'),
            )
        success = await service.rename_squad(squad_uuid, payload.name)
        detail = (
            texts.t('WEBAPI_REMNAWAVE_SQUAD_RENAME_SUCCESS', 'Сквад переименован')
            if success
            else texts.t('WEBAPI_REMNAWAVE_SQUAD_RENAME_FAILED', 'Не удалось переименовать сквад')
        )
    elif action == 'update_inbounds':
        if not payload.inbound_uuids:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                texts.t('WEBAPI_REMNAWAVE_INBOUNDS_REQUIRED', 'Необходимо указать inbound_uuids'),
            )
        success = await service.update_squad_inbounds(squad_uuid, payload.inbound_uuids)
        detail = (
            texts.t('WEBAPI_REMNAWAVE_INBOUNDS_UPDATE_SUCCESS', 'Инбаунды обновлены')
            if success
            else texts.t('WEBAPI_REMNAWAVE_INBOUNDS_UPDATE_FAILED', 'Не удалось обновить инбаунды')
        )

    return RemnaWaveOperationResponse(success=success, detail=detail)


@router.get('/inbounds', response_model=RemnaWaveInboundsResponse)
async def list_inbounds(
    _: Any = Security(require_api_token),
) -> RemnaWaveInboundsResponse:
    service = _get_service()
    _ensure_service_configured(service)

    inbounds = await service.get_all_inbounds()
    return RemnaWaveInboundsResponse(items=inbounds or [])


@router.get('/users/{telegram_id}/traffic', response_model=RemnaWaveUserTrafficResponse)
async def get_user_traffic(
    telegram_id: int,
    _: Any = Security(require_api_token),
) -> RemnaWaveUserTrafficResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    stats = await service.get_user_traffic_stats(telegram_id)
    if not stats:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_REMNAWAVE_USER_NOT_FOUND', 'Пользователь не найден в RemnaWave'),
        )

    return RemnaWaveUserTrafficResponse(telegram_id=telegram_id, **stats)


@router.get('/squads/{squad_uuid}/migration-preview', response_model=RemnaWaveSquadMigrationPreviewResponse)
async def preview_squad_migration(
    squad_uuid: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveSquadMigrationPreviewResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    squad = await get_server_squad_by_uuid(db, squad_uuid)
    if not squad:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t('WEBAPI_REMNAWAVE_SQUAD_NOT_FOUND', 'Сквад не найден'),
        )

    users_to_migrate = await count_active_users_for_squad(db, squad_uuid)

    return RemnaWaveSquadMigrationPreviewResponse(
        squad_uuid=squad.squad_uuid,
        squad_name=squad.display_name,
        current_users=squad.current_users or 0,
        max_users=squad.max_users,
        users_to_migrate=users_to_migrate,
    )


@router.post('/sync/from-panel', response_model=RemnaWaveGenericSyncResponse)
async def sync_from_panel(
    payload: RemnaWaveSyncFromPanelRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveGenericSyncResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    try:
        stats = await service.sync_users_from_panel(db, payload.mode)
        detail = texts.t('WEBAPI_REMNAWAVE_SYNC_FROM_PANEL_SUCCESS', 'Синхронизация из панели выполнена')
        return RemnaWaveGenericSyncResponse(success=True, detail=detail, data=stats)
    except Exception as exc:  # pragma: no cover - точный тип зависит от импорта
        if RemnaWaveConfigurationError and isinstance(exc, RemnaWaveConfigurationError):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        raise


@router.post('/sync/to-panel', response_model=RemnaWaveGenericSyncResponse)
async def sync_to_panel(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveGenericSyncResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    stats = await service.sync_users_to_panel(db)
    detail = texts.t('WEBAPI_REMNAWAVE_SYNC_TO_PANEL_SUCCESS', 'Синхронизация в панель выполнена')
    return RemnaWaveGenericSyncResponse(success=True, detail=detail, data=stats)


@router.post('/sync/subscriptions/validate', response_model=RemnaWaveGenericSyncResponse)
async def validate_and_fix_subscriptions(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveGenericSyncResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    stats = await service.validate_and_fix_subscriptions(db)
    detail = texts.t('WEBAPI_REMNAWAVE_SUBSCRIPTIONS_VALIDATED', 'Подписки проверены')
    return RemnaWaveGenericSyncResponse(success=True, detail=detail, data=stats)


@router.post('/sync/subscriptions/cleanup', response_model=RemnaWaveGenericSyncResponse)
async def cleanup_orphaned_subscriptions(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveGenericSyncResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    stats = await service.cleanup_orphaned_subscriptions(db)
    detail = texts.t('WEBAPI_REMNAWAVE_SUBSCRIPTIONS_CLEANUP_DONE', 'Очистка завершена')
    return RemnaWaveGenericSyncResponse(success=True, detail=detail, data=stats)


@router.post('/sync/subscriptions/statuses', response_model=RemnaWaveGenericSyncResponse)
async def sync_subscription_statuses(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveGenericSyncResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    stats = await service.sync_subscription_statuses(db)
    detail = texts.t(
        'WEBAPI_REMNAWAVE_SUBSCRIPTIONS_STATUSES_SYNCED',
        'Статусы подписок синхронизированы',
    )
    return RemnaWaveGenericSyncResponse(success=True, detail=detail, data=stats)


@router.get('/sync/recommendations', response_model=RemnaWaveGenericSyncResponse)
async def get_sync_recommendations(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveGenericSyncResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    data = await service.get_sync_recommendations(db)
    detail = texts.t('WEBAPI_REMNAWAVE_SYNC_RECOMMENDATIONS_RECEIVED', 'Рекомендации получены')
    return RemnaWaveGenericSyncResponse(success=True, detail=detail, data=data)


@router.post('/squads/migrate', response_model=RemnaWaveSquadMigrationResponse)
async def migrate_squad(
    payload: RemnaWaveSquadMigrationRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> RemnaWaveSquadMigrationResponse:
    texts = get_texts('ru')
    service = _get_service()
    _ensure_service_configured(service)

    source_uuid = payload.source_uuid.strip()
    target_uuid = payload.target_uuid.strip()

    if source_uuid == target_uuid:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t(
                'WEBAPI_REMNAWAVE_MIGRATION_SAME_SOURCE_TARGET',
                'Источник и назначение совпадают',
            ),
        )

    source = await get_server_squad_by_uuid(db, source_uuid)
    if not source:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t(
                'WEBAPI_REMNAWAVE_MIGRATION_SOURCE_NOT_FOUND',
                'Сквад-источник не найден',
            ),
        )

    target = await get_server_squad_by_uuid(db, target_uuid)
    if not target:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            texts.t(
                'WEBAPI_REMNAWAVE_MIGRATION_TARGET_NOT_FOUND',
                'Сквад-назначение не найден',
            ),
        )

    try:
        result = await service.migrate_squad_users(
            db,
            source_uuid=source.squad_uuid,
            target_uuid=target.squad_uuid,
        )
    except RemnaWaveConfigurationError as exc:  # pragma: no cover - зависит от окружения
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    if not result.get('success'):
        detail = result.get('message') or texts.t(
            'WEBAPI_REMNAWAVE_MIGRATION_FAILED',
            'Не удалось выполнить переезд',
        )
        return RemnaWaveSquadMigrationResponse(
            success=False,
            detail=detail,
            error=result.get('error'),
        )

    stats = RemnaWaveSquadMigrationStats(
        source_uuid=source.squad_uuid,
        target_uuid=target.squad_uuid,
        total=result.get('total', 0),
        updated=result.get('updated', 0),
        panel_updated=result.get('panel_updated', 0),
        panel_failed=result.get('panel_failed', 0),
        source_removed=result.get('source_removed', 0),
        target_added=result.get('target_added', 0),
    )

    detail = result.get('message') or texts.t(
        'WEBAPI_REMNAWAVE_MIGRATION_SUCCESS',
        'Переезд выполнен',
    )
    return RemnaWaveSquadMigrationResponse(success=True, detail=detail, data=stats)
