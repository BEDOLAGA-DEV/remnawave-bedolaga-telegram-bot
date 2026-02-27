"""Маршруты административного API для просмотра логов."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD
from app.localization.texts import get_texts
from app.services.monitoring_service import monitoring_service

from ..dependencies import get_db_session, require_api_token
from ..schemas.logs import (
    MonitoringLogEntry,
    MonitoringLogListResponse,
    MonitoringLogTypeListResponse,
    SupportAuditActionsResponse,
    SupportAuditLogEntry,
    SupportAuditLogListResponse,
    SystemLogFullResponse,
    SystemLogPreviewResponse,
)


router = APIRouter()

logger = structlog.get_logger(__name__)


SYSTEM_LOG_PREVIEW_LIMIT_DEFAULT = 4000
SYSTEM_LOG_PREVIEW_LIMIT_MAX = 20000


def _resolve_system_log_path() -> Path:
    path = Path(settings.LOG_FILE)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


async def _read_system_log(path: Path) -> tuple[str, int, float | None]:
    def _read() -> tuple[str, int, float]:
        content = path.read_text(encoding='utf-8', errors='ignore')
        stats = path.stat()
        return content, stats.st_size, stats.st_mtime

    return await run_in_threadpool(_read)


def _format_timestamp(timestamp: float | None) -> datetime | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


@router.get('/system', response_model=SystemLogPreviewResponse)
async def get_system_log_preview(
    _: Any = Security(require_api_token),
    preview_limit: int = Query(
        SYSTEM_LOG_PREVIEW_LIMIT_DEFAULT,
        ge=500,
        le=SYSTEM_LOG_PREVIEW_LIMIT_MAX,
        description='Количество символов предпросмотра от конца файла',
    ),
) -> SystemLogPreviewResponse:
    """Получить предпросмотр системного лог-файла бота."""

    texts = get_texts('ru')
    log_path = _resolve_system_log_path()

    if not log_path.exists() or not log_path.is_file():
        return SystemLogPreviewResponse(
            path=str(log_path),
            exists=False,
            updated_at=None,
            size_bytes=0,
            size_chars=0,
            preview='',
            preview_chars=0,
            preview_truncated=False,
            download_url='/logs/system/download',
        )

    try:
        content, size_bytes, mtime = await _read_system_log(log_path)
    except FileNotFoundError:
        logger.warning('Лог-файл исчез во время чтения', log_path=log_path)
        return SystemLogPreviewResponse(
            path=str(log_path),
            exists=False,
            updated_at=None,
            size_bytes=0,
            size_chars=0,
            preview='',
            preview_chars=0,
            preview_truncated=False,
            download_url='/logs/system/download',
        )
    except Exception as error:  # pragma: no cover - защита от неожиданных ошибок чтения
        logger.error('Ошибка чтения лог-файла', log_path=log_path, error=error)
        raise HTTPException(
            status_code=500,
            detail=texts.t(
                'WEBAPI_LOGS_FAILED_TO_READ_LOG_FILE',
                'Не удалось прочитать лог-файл',
            ),
        ) from error

    preview_text = content[-preview_limit:] if preview_limit > 0 else ''
    truncated = len(content) > len(preview_text)

    return SystemLogPreviewResponse(
        path=str(log_path),
        exists=True,
        updated_at=_format_timestamp(mtime),
        size_bytes=size_bytes,
        size_chars=len(content),
        preview=preview_text,
        preview_chars=len(preview_text),
        preview_truncated=truncated,
        download_url='/logs/system/download',
    )


@router.get('/system/download')
async def download_system_log(
    _: Any = Security(require_api_token),
) -> FileResponse:
    """Скачать полный лог-файл бота."""

    texts = get_texts('ru')
    log_path = _resolve_system_log_path()

    if not log_path.exists() or not log_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=texts.t(
                'WEBAPI_LOGS_LOG_FILE_NOT_FOUND',
                'Лог-файл не найден',
            ),
        )

    try:
        return FileResponse(
            log_path,
            media_type='text/plain',
            filename=log_path.name,
        )
    except Exception as error:  # pragma: no cover - защита от неожиданных ошибок отдачи файла
        logger.error('Ошибка отправки лог-файла', log_path=log_path, error=error)
        raise HTTPException(
            status_code=500,
            detail=texts.t(
                'WEBAPI_LOGS_FAILED_TO_SEND_LOG_FILE',
                'Не удалось отправить лог-файл',
            ),
        ) from error


@router.get('/system/full', response_model=SystemLogFullResponse)
async def get_system_log_full(
    _: Any = Security(require_api_token),
) -> SystemLogFullResponse:
    """Получить полный системный лог-файл бота."""

    texts = get_texts('ru')
    log_path = _resolve_system_log_path()

    if not log_path.exists() or not log_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=texts.t(
                'WEBAPI_LOGS_LOG_FILE_NOT_FOUND',
                'Лог-файл не найден',
            ),
        )

    try:
        content, size_bytes, mtime = await _read_system_log(log_path)
    except Exception as error:  # pragma: no cover - защита от неожиданных ошибок чтения
        logger.error('Ошибка чтения лог-файла', log_path=log_path, error=error)
        raise HTTPException(
            status_code=500,
            detail=texts.t(
                'WEBAPI_LOGS_FAILED_TO_READ_LOG_FILE',
                'Не удалось прочитать лог-файл',
            ),
        ) from error

    return SystemLogFullResponse(
        path=str(log_path),
        exists=True,
        updated_at=_format_timestamp(mtime),
        size_bytes=size_bytes,
        size_chars=len(content),
        content=content,
    )


@router.get('/monitoring', response_model=MonitoringLogListResponse)
async def list_monitoring_logs(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200, description='Количество записей на странице'),
    offset: int = Query(0, ge=0, description='Смещение от начала списка'),
    event_type: str | None = Query(
        default=None,
        max_length=100,
        description='Фильтр по типу события',
    ),
) -> MonitoringLogListResponse:
    """Получить список логов мониторинга с пагинацией."""

    per_page = limit
    page = (offset // per_page) + 1

    raw_logs = await monitoring_service.get_monitoring_logs(
        db,
        event_type=event_type,
        page=page,
        per_page=per_page,
    )
    total = await monitoring_service.get_monitoring_logs_count(db, event_type=event_type)

    return MonitoringLogListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[MonitoringLogEntry(**entry) for entry in raw_logs],
    )


@router.get('/monitoring/event-types', response_model=MonitoringLogTypeListResponse)
async def list_monitoring_event_types(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> MonitoringLogTypeListResponse:
    """Получить список доступных типов событий мониторинга."""

    event_types = await monitoring_service.get_monitoring_event_types(db)
    return MonitoringLogTypeListResponse(items=event_types)


@router.get('/support', response_model=SupportAuditLogListResponse)
async def list_support_audit_logs(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200, description='Количество записей на странице'),
    offset: int = Query(0, ge=0, description='Смещение от начала списка'),
    action: str | None = Query(
        default=None,
        max_length=50,
        description='Фильтр по типу действия модератора',
    ),
) -> SupportAuditLogListResponse:
    """Получить список аудита действий модераторов поддержки."""

    logs = await TicketCRUD.list_support_audit(
        db,
        limit=limit,
        offset=offset,
        action=action,
    )
    total = await TicketCRUD.count_support_audit(db, action=action)

    return SupportAuditLogListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            SupportAuditLogEntry(
                id=log.id,
                actor_user_id=log.actor_user_id,
                actor_telegram_id=log.actor_telegram_id,
                is_moderator=log.is_moderator,
                action=log.action,
                ticket_id=log.ticket_id,
                target_user_id=log.target_user_id,
                details=log.details,
                created_at=log.created_at,
            )
            for log in logs
        ],
    )


@router.get('/support/actions', response_model=SupportAuditActionsResponse)
async def list_support_audit_actions(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SupportAuditActionsResponse:
    """Получить список действий, доступных в аудите поддержки."""

    actions = await TicketCRUD.list_support_audit_actions(db)
    return SupportAuditActionsResponse(items=actions)
