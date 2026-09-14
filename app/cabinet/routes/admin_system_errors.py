"""Admin system errors routes — журнал ошибок приложения и их доставки.

Второй, независимый от Telegram канал: кабинет живёт на том же хосте, что и
бот, и доступен напрямую, минуя прокси-пул. Поэтому когда все пути до
Telegram лежат, ошибки всё равно видно здесь.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

import structlog
from aiogram import Bot
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.config import settings
from app.database.crud.system_errors import (
    get_error_event,
    get_error_summary,
    list_error_events,
    mark_delivery_result,
)
from app.database.models import SystemErrorEvent, User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/system-errors', tags=['Admin System Errors'])


# ============ Schemas ============


class SystemErrorListItem(BaseModel):
    """Строка списка — без трейсбека, чтобы не раздувать ответ."""

    id: int
    created_at: datetime | None = None
    level: str
    logger_name: str | None = None
    event: str
    error_type: str | None = None
    user_id: int | None = None
    delivery_status: str
    delivery_attempts: int
    delivered_at: datetime | None = None
    has_traceback: bool = False


class SystemErrorDetail(SystemErrorListItem):
    """Полная запись, включая трейсбек и контекст."""

    traceback: str | None = None
    context: dict[str, Any] | None = None
    last_attempt_at: datetime | None = None
    delivery_error: str | None = None
    dedup_hash: str | None = None


class SystemErrorListResponse(BaseModel):
    items: list[SystemErrorListItem]
    total: int
    limit: int
    offset: int


class SystemErrorSummary(BaseModel):
    undelivered_total: int
    last_24h: int
    last_7d: int
    by_status_7d: dict[str, int]
    top_errors_7d: list[dict[str, Any]]


# ============ Helpers ============

_cached_bot: Bot | None = None

# Telegram режет caption у документа на 1024 символах — длинный трейсбек
# уезжает в приложенный файл, как и в обычном отчёте об ошибке.
CAPTION_LIMIT = 900


def _get_bot() -> Bot:
    global _cached_bot
    if _cached_bot is None:
        _cached_bot = create_bot()
    return _cached_bot


def _build_retry_message(event: SystemErrorEvent) -> str:
    created = event.created_at.isoformat() if event.created_at else '—'
    parts = [
        '<b>Повторная отправка ошибки</b>',
        '',
        f'<b>Тип:</b> <code>{html.escape(event.error_type or "—")}</code>',
        f'<b>Логгер:</b> <code>{html.escape(event.logger_name or "—")}</code>',
        f'<b>Когда:</b> {html.escape(created)}',
        '',
        f'<code>{html.escape(event.event or "")[:CAPTION_LIMIT]}</code>',
    ]
    return '\n'.join(parts)


async def _resend_to_admin_chat(event: SystemErrorEvent) -> None:
    """Отправить сохранённую ошибку в админ-чат. Бросает при неудаче."""
    chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
    if not chat_id:
        raise RuntimeError('ADMIN_NOTIFICATIONS_CHAT_ID не настроен')

    topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', None) or getattr(
        settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None
    )
    kwargs: dict[str, Any] = {'chat_id': chat_id, 'parse_mode': 'HTML'}
    if topic_id:
        kwargs['message_thread_id'] = topic_id

    bot = _get_bot()
    text = _build_retry_message(event)

    if event.traceback:
        document = BufferedInputFile(
            file=event.traceback.encode('utf-8'),
            filename=f'error_{event.id}.txt',
        )
        await bot.send_document(document=document, caption=text, **kwargs)
    else:
        await bot.send_message(text=text, disable_web_page_preview=True, **kwargs)


def _to_detail(event: SystemErrorEvent) -> SystemErrorDetail:
    base = _to_list_item(event)
    return SystemErrorDetail(
        **base.model_dump(),
        traceback=event.traceback,
        context=event.context,
        last_attempt_at=event.last_attempt_at,
        delivery_error=event.delivery_error,
        dedup_hash=event.dedup_hash,
    )


def _to_list_item(event) -> SystemErrorListItem:
    return SystemErrorListItem(
        id=event.id,
        created_at=event.created_at,
        level=event.level,
        logger_name=event.logger_name,
        event=event.event,
        error_type=event.error_type,
        user_id=event.user_id,
        delivery_status=event.delivery_status,
        delivery_attempts=event.delivery_attempts,
        delivered_at=event.delivered_at,
        has_traceback=bool(event.traceback),
    )


# ============ Routes ============


@router.get('/summary', response_model=SystemErrorSummary)
async def system_errors_summary(
    admin: User = Depends(require_permission('system_errors:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Сводка для бейджа в шапке и верхнего блока страницы."""
    summary = await get_error_summary(db)
    return SystemErrorSummary(**summary)


@router.get('', response_model=SystemErrorListResponse)
async def list_system_errors(
    admin: User = Depends(require_permission('system_errors:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    level: str | None = Query(default=None),
    delivery_status: str | None = Query(default=None),
    logger_name: str | None = Query(default=None),
    search: str | None = Query(default=None),
    undelivered_only: bool = Query(default=False),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Список ошибок с фильтрами по уровню, статусу доставки и периоду."""
    events, total = await list_error_events(
        db,
        level=level,
        delivery_status=delivery_status,
        logger_name=logger_name,
        search=search,
        undelivered_only=undelivered_only,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )

    return SystemErrorListResponse(
        items=[_to_list_item(event) for event in events],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/{event_id}', response_model=SystemErrorDetail)
async def get_system_error(
    event_id: int,
    admin: User = Depends(require_permission('system_errors:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Полная запись с трейсбеком и контекстом."""
    event = await get_error_event(db, event_id)
    if not event:
        raise HTTPException(status_code=404, detail='Error event not found')

    return _to_detail(event)


@router.post('/{event_id}/retry', response_model=SystemErrorDetail)
async def retry_system_error_delivery(
    event_id: int,
    admin: User = Depends(require_permission('system_errors:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Повторно отправить сохранённую ошибку в админ-чат.

    В отличие от автоматического пути, здесь нет троттлинга и дедупликации —
    админ жмёт кнопку осознанно.
    """
    event = await get_error_event(db, event_id)
    if not event:
        raise HTTPException(status_code=404, detail='Error event not found')

    try:
        await _resend_to_admin_chat(event)
    except Exception as e:
        # ВАЖНО: warning, не error. logger.error отсюда уйдёт в
        # TelegramNotifierProcessor и породит новую запись об ошибке
        # прямо во время разбора старой.
        logger.warning(
            'Повторная отправка ошибки в админ-чат не удалась',
            event_id=event_id,
            error=str(e)[:200],
        )
        await mark_delivery_result(db, event, delivered=False, error=str(e))
        return _to_detail(event)

    await mark_delivery_result(db, event, delivered=True)
    return _to_detail(event)
