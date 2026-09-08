"""Pydantic-схемы для работы с логами административного API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.localization.texts import get_texts


class MonitoringLogEntry(BaseModel):
    """Запись лога мониторинга."""

    id: int
    event_type: str = Field(
        ..., description=get_texts().t('MONITORING_LOG_EVENT_TYPE_DESCRIPTION', 'Тип события мониторинга')
    )
    message: str = Field(
        ..., description=get_texts().t('MONITORING_LOG_MESSAGE_DESCRIPTION', 'Краткое описание события')
    )
    data: dict[str, Any] | None = Field(
        default=None,
        description=get_texts().t('MONITORING_LOG_DATA_DESCRIPTION', 'Дополнительные данные события'),
    )
    is_success: bool = Field(
        ..., description=get_texts().t('MONITORING_LOG_IS_SUCCESS_DESCRIPTION', 'Флаг успешности выполнения операции')
    )
    created_at: datetime = Field(
        ..., description=get_texts().t('MONITORING_LOG_CREATED_AT_DESCRIPTION', 'Дата и время создания записи')
    )


class MonitoringLogListResponse(BaseModel):
    """Ответ со списком логов мониторинга."""

    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)
    items: list[MonitoringLogEntry]


class MonitoringLogTypeListResponse(BaseModel):
    """Ответ со списком доступных типов событий мониторинга."""

    items: list[str] = Field(default_factory=list)


class SupportAuditLogEntry(BaseModel):
    """Запись аудита модераторов поддержки."""

    id: int
    actor_user_id: int | None
    actor_telegram_id: int | None = None
    is_moderator: bool
    action: str
    ticket_id: int | None
    target_user_id: int | None
    details: dict[str, Any] | None = None
    created_at: datetime


class SupportAuditLogListResponse(BaseModel):
    """Ответ со списком аудита поддержки."""

    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)
    items: list[SupportAuditLogEntry]


class SupportAuditActionsResponse(BaseModel):
    """Ответ со списком доступных действий аудита поддержки."""

    items: list[str] = Field(default_factory=list)


class SystemLogPreviewResponse(BaseModel):
    """Ответ с превью системного лог-файла бота."""

    path: str = Field(..., description=get_texts().t('SYSTEM_LOG_PATH_DESCRIPTION', 'Абсолютный путь до лог-файла'))
    exists: bool = Field(..., description=get_texts().t('SYSTEM_LOG_EXISTS_DESCRIPTION', 'Флаг наличия лог-файла'))
    updated_at: datetime | None = Field(
        default=None,
        description=get_texts().t(
            'SYSTEM_LOG_UPDATED_AT_DESCRIPTION',
            'Дата и время последнего изменения лог-файла',
        ),
    )
    size_bytes: int = Field(
        ..., ge=0, description=get_texts().t('SYSTEM_LOG_SIZE_BYTES_DESCRIPTION', 'Размер лог-файла в байтах')
    )
    size_chars: int = Field(
        ..., ge=0, description=get_texts().t('SYSTEM_LOG_SIZE_CHARS_DESCRIPTION', 'Количество символов в лог-файле')
    )
    preview: str = Field(
        default='',
        description=get_texts().t(
            'SYSTEM_LOG_PREVIEW_DESCRIPTION',
            'Фрагмент содержимого лог-файла, возвращаемый для предпросмотра',
        ),
    )
    preview_chars: int = Field(
        ..., ge=0, description=get_texts().t('SYSTEM_LOG_PREVIEW_CHARS_DESCRIPTION', 'Размер предпросмотра в символах')
    )
    preview_truncated: bool = Field(
        ...,
        description=get_texts().t(
            'SYSTEM_LOG_PREVIEW_TRUNCATED_DESCRIPTION',
            'Флаг усечения предпросмотра относительно полного файла',
        ),
    )
    download_url: str | None = Field(
        default=None,
        description=get_texts().t(
            'SYSTEM_LOG_DOWNLOAD_URL_DESCRIPTION',
            'Относительный путь до endpoint для скачивания лог-файла',
        ),
    )


class SystemLogFullResponse(BaseModel):
    """Полное содержимое системного лог-файла."""

    path: str
    exists: bool
    updated_at: datetime | None = None
    size_bytes: int
    size_chars: int
    content: str
