"""Очередь писем в админке: посмотреть состояние и очистить.

Очередь повторной отправки жила невидимкой: увидеть, сколько писем ждёт
отправки, и убрать их можно было только запросом в базу. Когда почтовый
сервер не настроен вовсе, это особенно неудобно — письма копятся, а владелец
про них узнаёт только из сообщений об ошибках.

Тело письма наружу не отдаётся никогда: внутри коды подтверждения и ссылки
входа, а список нужен для «кому и что не ушло», а не для чтения писем.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import EmailQueueItem, User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/email-queue', tags=['Admin Email Queue'])

# Сколько последних писем показываем в списке.
RECENT_LIMIT = 50


def _serialize(item: EmailQueueItem) -> dict[str, Any]:
    return {
        'id': item.id,
        'to_email': item.to_email,
        'subject': item.subject,
        'status': item.status,
        'attempts': item.attempts,
        'next_attempt_at': item.next_attempt_at,
        'last_error': item.last_error,
        'created_at': item.created_at,
        'sent_at': item.sent_at,
    }


@router.get('', summary='Email queue state')
async def get_email_queue(
    admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Сводка по очереди писем и последние письма в ней."""
    counts_result = await db.execute(
        select(EmailQueueItem.status, func.count(EmailQueueItem.id)).group_by(EmailQueueItem.status)
    )
    counts = dict(counts_result.all())

    rows = (
        (
            await db.execute(
                select(EmailQueueItem)
                .order_by(EmailQueueItem.created_at.desc(), EmailQueueItem.id.desc())
                .limit(RECENT_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    return {
        'pending': counts.get('pending', 0),
        'sent': counts.get('sent', 0),
        'dead': counts.get('dead', 0),
        # Без сервера очередь бессмысленна: кабинет объясняет это словами, а не
        # показывает счётчики как аварию.
        'smtp_configured': settings.is_smtp_configured(),
        'items': [_serialize(row) for row in rows],
    }


@router.delete('', summary='Clear email queue')
async def clear_email_queue(
    pending_only: bool = Query(False, description='Убрать только ожидающие отправки, оставив историю'),
    admin: User = Depends(require_permission('email_templates:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Убрать письма из очереди. По умолчанию — целиком, вместе с историей."""
    statement = delete(EmailQueueItem)
    if pending_only:
        statement = statement.where(EmailQueueItem.status == 'pending')

    result = await db.execute(statement)
    await db.commit()

    removed = result.rowcount or 0
    logger.info(
        'Очередь писем очищена администратором',
        admin_id=getattr(admin, 'id', None),
        removed=removed,
        pending_only=pending_only,
    )
    return {'removed': removed, 'pending_only': pending_only}
