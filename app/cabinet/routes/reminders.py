"""Напоминания пользователя в кабинете: карточки на главной и закрытие."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.user_reminders.cabinet import active_cards_for_user, dismiss_reminder

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.user_reminders import ReminderCard


router = APIRouter(prefix='/reminders', tags=['Cabinet Reminders'])


@router.get('/active', response_model=list[ReminderCard])
async def get_active_reminders(
    lang: str = Query('ru', max_length=8),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    return await active_cards_for_user(db, user, lang=lang)


@router.post('/{reminder_id}/dismiss', status_code=status.HTTP_204_NO_CONTENT)
async def dismiss(
    reminder_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    if not await dismiss_reminder(db, user, reminder_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Reminder not found')
    return Response(status_code=status.HTTP_204_NO_CONTENT)
