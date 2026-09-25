"""Антифрод в кабинете: предупреждение клиенту и картина для оператора.

Два адресата — два объёма. Клиент видит только предупреждение, которое ему уже
отправили: то же сообщение, что пришло в Telegram, и ничего сверх него.
Оператор видит вердикт и историю нарушений.

Разница не в вежливости, а в последствиях: перечень сработавших признаков на
руках у нарушителя — готовая инструкция по обходу, человек просто разнесёт
подключения по разным сетям и устройствам. Поэтому клиентская ручка физически
не умеет отдавать ни скоринг, ни виды нарушений.

Внешний сервис необязателен: не настроен или не ответил — экраны работают как
раньше, просто без отметок.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import get_user_by_id
from app.database.models import User
from app.services import abuse_api_service

from ..dependencies import get_cabinet_db, get_current_cabinet_user, require_permission


router = APIRouter(tags=['Cabinet Abuse'])


class AbuseNoticeResponse(BaseModel):
    """Предупреждение в том виде, в каком его показывают клиенту."""

    subject: str | None = None
    body: str | None = None
    sent_at: str | None = None


class AbuseStatusResponse(BaseModel):
    """Ответ клиенту: есть ли к нему вопросы и что именно ему написали."""

    warned: bool = False
    notice: AbuseNoticeResponse | None = None


class AbuseViolationResponse(BaseModel):
    detected_at: str | None = None
    score: float | None = None
    recommended_action: str | None = None
    action_taken: str | None = None
    reasons: list[str] | None = None
    notified_at: str | None = None


class AbuseOverviewResponse(BaseModel):
    """Ответ оператору: вердикт и история."""

    available: bool = False
    level: str | None = None
    violations_count: int = 0
    max_score: float | None = None
    last_detected_at: str | None = None
    whitelisted: bool = False
    violations: list[AbuseViolationResponse] = []


@router.get('/abuse-status', response_model=AbuseStatusResponse)
async def my_abuse_status(user: User = Depends(get_current_cabinet_user)):
    """Предупреждение для самого клиента.

    Отдаём только текст, который человек уже получил. Уровень, скоринг и виды
    нарушений сюда не попадают — ни в каком поле.
    """
    if not user.telegram_id:
        return AbuseStatusResponse()

    summary = await abuse_api_service.get_summary(user.telegram_id)
    notice = (summary or {}).get('notice') or None
    if not notice or not notice.get('body'):
        return AbuseStatusResponse()

    return AbuseStatusResponse(
        warned=True,
        notice=AbuseNoticeResponse(
            subject=notice.get('subject'),
            body=notice.get('body'),
            sent_at=notice.get('sent_at'),
        ),
    )


@router.get('/admin/users/{user_id}/abuse', response_model=AbuseOverviewResponse)
async def user_abuse_overview(
    user_id: int,
    admin: User = Depends(require_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Вердикт и история нарушений клиента — для оператора."""
    target = await get_user_by_id(db, user_id)
    if not target or not target.telegram_id or not abuse_api_service.is_configured():
        return AbuseOverviewResponse()

    summary = await abuse_api_service.get_summary(target.telegram_id)
    if not summary:
        return AbuseOverviewResponse()

    violations = await abuse_api_service.get_violations(target.telegram_id)
    return AbuseOverviewResponse(
        available=True,
        level=summary.get('level'),
        violations_count=int(summary.get('violations') or 0),
        max_score=summary.get('max_score'),
        last_detected_at=summary.get('last_detected_at'),
        whitelisted=bool(summary.get('whitelisted')),
        violations=[
            AbuseViolationResponse(
                detected_at=item.get('detected_at'),
                score=item.get('score'),
                recommended_action=item.get('recommended_action'),
                action_taken=item.get('action_taken'),
                reasons=item.get('reasons'),
                notified_at=item.get('notified_at'),
            )
            for item in violations
        ],
    )
