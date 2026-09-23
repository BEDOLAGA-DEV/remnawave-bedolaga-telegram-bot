"""Админ-раздел «Напоминания»: CRUD, счётчик аудитории, тестовая отправка себе."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from aiogram.enums import ParseMode
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.crud.user_reminder import count_audience, get_reminder, list_reminders, reminder_stats
from app.database.models import User, UserReminder
from app.services.user_reminders.conditions import ReminderConditions, parse_conditions
from app.services.user_reminders.texts import render_bot_message, validate_texts

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.user_reminders import (
    AudienceRequest,
    AudienceResponse,
    ReminderPayload,
    ReminderResponse,
    ReminderStats,
)


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/reminders', tags=['Cabinet Admin Reminders'])


async def _audience(
    db: AsyncSession, conditions: ReminderConditions, channels: str, category: str = 'service'
) -> AudienceResponse:
    now = datetime.now(UTC)
    exclude_promo_opt_out = category == 'marketing'
    return AudienceResponse(
        bot=(
            await count_audience(
                db, conditions, now=now, telegram_only=True, exclude_promo_opt_out=exclude_promo_opt_out
            )
            if channels in ('bot', 'both')
            else None
        ),
        cabinet=(
            await count_audience(db, conditions, now=now, telegram_only=False)
            if channels in ('cabinet', 'both')
            else None
        ),
    )


async def _response(db: AsyncSession, reminder: UserReminder, stats: dict | None = None) -> ReminderResponse:
    counters = (stats or {}).get(reminder.id, {})
    try:
        audience = await _audience(db, parse_conditions(reminder.conditions), reminder.channels, reminder.category)
    except ValueError:
        audience = AudienceResponse()
    return ReminderResponse.model_validate(
        {
            'id': reminder.id,
            'name': reminder.name,
            'channels': reminder.channels,
            'category': reminder.category,
            'conditions': reminder.conditions or {},
            'repeat_every_days': reminder.repeat_every_days,
            'max_sends': reminder.max_sends,
            'texts': reminder.texts,
            'button_kind': reminder.button_kind,
            'button_target': reminder.button_target,
            'is_active': reminder.is_active,
            'is_builtin': reminder.is_builtin,
            'created_at': reminder.created_at,
            'updated_at': reminder.updated_at,
            'stats': ReminderStats(
                sent_total=counters.get('sent_total', 0),
                dismissed_total=counters.get('dismissed_total', 0),
                audience_bot=audience.bot,
                audience_cabinet=audience.cabinet,
            ),
        }
    )


async def _require(db: AsyncSession, reminder_id: int) -> UserReminder:
    reminder = await get_reminder(db, reminder_id)
    if reminder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Reminder not found')
    return reminder


def _apply(reminder: UserReminder, payload: ReminderPayload) -> None:
    reminder.name = payload.name
    reminder.channels = payload.channels
    reminder.category = payload.category
    reminder.conditions = payload.conditions.model_dump(exclude_none=True)
    reminder.repeat_every_days = payload.repeat_every_days
    reminder.max_sends = payload.max_sends
    reminder.texts = payload.texts
    reminder.button_kind = payload.button_kind
    reminder.button_target = payload.button_target if payload.button_kind != 'none' else None
    reminder.updated_at = datetime.now(UTC)


@router.get('', response_model=list[ReminderResponse])
async def list_reminders_route(
    admin: User = Depends(require_permission('user_reminders:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    stats = await reminder_stats(db)
    return [await _response(db, reminder, stats) for reminder in await list_reminders(db)]


@router.post('/audience', response_model=AudienceResponse)
async def audience(
    request: AudienceRequest,
    admin: User = Depends(require_permission('user_reminders:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    return await _audience(db, request.conditions, request.channels, request.category)


@router.get('/{reminder_id}', response_model=ReminderResponse)
async def get_reminder_route(
    reminder_id: int,
    admin: User = Depends(require_permission('user_reminders:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    reminder = await _require(db, reminder_id)
    return await _response(db, reminder, await reminder_stats(db))


@router.post('', response_model=ReminderResponse, status_code=status.HTTP_201_CREATED)
async def create_reminder(
    payload: ReminderPayload,
    admin: User = Depends(require_permission('user_reminders:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    reminder = UserReminder(is_active=False)
    _apply(reminder, payload)
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)
    logger.info('Создано напоминание', reminder_id=reminder.id, admin_id=admin.id)
    return await _response(db, reminder)


@router.put('/{reminder_id}', response_model=ReminderResponse)
async def update_reminder(
    reminder_id: int,
    payload: ReminderPayload,
    admin: User = Depends(require_permission('user_reminders:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    reminder = await _require(db, reminder_id)
    _apply(reminder, payload)
    await db.commit()
    await db.refresh(reminder)
    return await _response(db, reminder, await reminder_stats(db))


@router.post('/{reminder_id}/toggle', response_model=ReminderResponse)
async def toggle_reminder(
    reminder_id: int,
    admin: User = Depends(require_permission('user_reminders:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    reminder = await _require(db, reminder_id)
    reminder.is_active = not reminder.is_active
    reminder.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(reminder)
    logger.info('Напоминание переключено', reminder_id=reminder.id, is_active=reminder.is_active, admin_id=admin.id)
    return await _response(db, reminder, await reminder_stats(db))


@router.delete('/{reminder_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_reminder(
    reminder_id: int,
    admin: User = Depends(require_permission('user_reminders:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    reminder = await _require(db, reminder_id)
    if reminder.is_builtin:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Built-in reminder can only be disabled')
    await db.delete(reminder)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/{reminder_id}/test')
async def send_test(
    reminder_id: int,
    admin: User = Depends(require_permission('user_reminders:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    reminder = await _require(db, reminder_id)
    if not getattr(admin, 'telegram_id', None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Admin has no Telegram account')
    try:
        validate_texts(reminder.texts)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail='Reminder texts are invalid — fix them before testing',
        ) from error
    text, markup = render_bot_message(reminder, getattr(admin, 'language', None))
    bot = create_bot()
    try:
        await bot.send_message(chat_id=admin.telegram_id, text=text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception as error:
        logger.warning('Тестовое напоминание не отправлено', reminder_id=reminder.id, error=str(error))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail='Telegram rejected the message') from error
    finally:
        await bot.session.close()
    return {'ok': True}
