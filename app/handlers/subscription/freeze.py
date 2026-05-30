from __future__ import annotations

from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User


async def handle_freeze_subscription(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
) -> None:
    from app.database.crud.subscription import get_active_subscriptions_by_user_id
    from app.services.freeze_service import FreezeError, FreezeService

    subs = await get_active_subscriptions_by_user_id(db, db_user.id)
    if not subs:
        await callback.answer('Нет активной подписки', show_alert=True)
        return

    try:
        await FreezeService().freeze_subscription(db, subs[0], db_user)
    except FreezeError as e:
        await callback.answer(e.message, show_alert=True)
        return

    await callback.answer('❄️ Подписка заморожена')
    try:
        from app.handlers.subscription.purchase import show_subscription_info

        await show_subscription_info(callback, db_user, db)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc):
            raise


async def handle_resume_subscription(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
) -> None:
    from app.database.crud.subscription import get_active_subscriptions_by_user_id
    from app.services.freeze_service import FreezeError, FreezeService

    subs = await get_active_subscriptions_by_user_id(db, db_user.id)
    if not subs:
        await callback.answer('Нет подписки', show_alert=True)
        return

    try:
        await FreezeService().resume_subscription(db, subs[0], db_user, reason='manual')
    except FreezeError as e:
        await callback.answer(e.message, show_alert=True)
        return

    await callback.answer('▶️ Подписка разморожена')
    try:
        from app.handlers.subscription.purchase import show_subscription_info

        await show_subscription_info(callback, db_user, db)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc):
            raise
