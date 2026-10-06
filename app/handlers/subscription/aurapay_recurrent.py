"""Telegram controls for AuraPay card renewal."""

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.aurapay_recurrent import cancel, enable, get_active

from .autopay import _resolve_subscription


async def handle_aurapay_recurrent_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext = None,
):
    texts = get_texts(db_user.language)
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        await callback.answer('Подписка не найдена', show_alert=True)
        return
    record = await get_active(db, subscription.id)
    if record is None and not settings.is_aurapay_recurrent_enabled():
        await callback.answer('Автопродление AuraPay недоступно', show_alert=True)
        return
    if record is None:
        status_text = 'не подключено'
        action = types.InlineKeyboardButton(text='✅ Подключить', callback_data='aurapay_recurrent_enable')
    else:
        status_text = {
            'NEW': 'ожидает оплаты',
            'WAITING_PAYMENT': 'ожидает повторной оплаты',
            'ACTIVE': 'активно',
        }.get(record.status, record.status)
        action = types.InlineKeyboardButton(text='❌ Отключить', callback_data='aurapay_recurrent_cancel')
    await callback.message.edit_text(
        f'💳 <b>Автопродление AuraPay</b>\n\nСтатус: {status_text}',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [action],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='subscription_autopay')],
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_aurapay_recurrent_enable(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext = None,
):
    if not settings.is_aurapay_recurrent_enabled():
        await callback.answer('Автопродление AuraPay недоступно', show_alert=True)
        return
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        await callback.answer('Подписка не найдена', show_alert=True)
        return
    await db.refresh(subscription, ['tariff'])
    if subscription.tariff is None:
        await callback.answer('Для подписки не выбран тариф', show_alert=True)
        return
    try:
        record = await enable(db, user=db_user, subscription=subscription, tariff=subscription.tariff)
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    except Exception:
        await callback.answer('Не удалось подключить автопродление', show_alert=True)
        return
    if not record.redirect_url:
        await handle_aurapay_recurrent_menu(callback, db_user, db, state)
        return
    await callback.message.edit_text(
        '💳 <b>Автопродление AuraPay</b>\n\nОплатите первый период картой для подключения.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='💳 Перейти к оплате', url=record.redirect_url)],
                [types.InlineKeyboardButton(text='Назад', callback_data='aurapay_recurrent_menu')],
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


async def handle_aurapay_recurrent_cancel(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext = None,
):
    subscription, _ = await _resolve_subscription(callback, db_user, db, state)
    if subscription is None:
        await callback.answer('Подписка не найдена', show_alert=True)
        return
    record = await get_active(db, subscription.id)
    if record is None:
        await callback.answer('Автопродление уже отключено', show_alert=True)
        return
    try:
        await cancel(db, record)
    except Exception:
        await callback.answer('Не удалось подтвердить отмену', show_alert=True)
        return
    await callback.answer('Автопродление отключено')
    if settings.is_aurapay_recurrent_enabled():
        await handle_aurapay_recurrent_menu(callback, db_user, db, state)
