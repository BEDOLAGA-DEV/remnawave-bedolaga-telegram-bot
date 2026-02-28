"""Обработчики пополнения баланса через SHKeeper."""

from __future__ import annotations

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService, get_user_by_id as fetch_user_by_id
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_shkeeper_payment(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if not settings.is_shkeeper_enabled():
        await callback.answer('❌ Оплата через SHKeeper временно недоступна', show_alert=True)
        return

    keyboard = get_back_keyboard(db_user.language)
    if settings.is_quick_amount_buttons_enabled():
        from .main import get_quick_amount_buttons

        quick_amount_buttons = await get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard

    await callback.message.edit_text(
        (
            '💳 <b>Оплата через SHKeeper</b>\n\n'
            f'Введите сумму пополнения.\n'
            f'Минимум: {settings.format_price(settings.SHKEEPER_MIN_AMOUNT_KOPEKS)}\n'
            f'Максимум: {settings.format_price(settings.SHKEEPER_MAX_AMOUNT_KOPEKS)}'
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='shkeeper',
        shkeeper_prompt_message_id=callback.message.message_id,
        shkeeper_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_shkeeper_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if not settings.is_shkeeper_enabled():
        await message.answer('❌ Оплата через SHKeeper временно недоступна')
        return

    if amount_kopeks < settings.SHKEEPER_MIN_AMOUNT_KOPEKS:
        await message.answer(f'Минимальная сумма пополнения: {settings.format_price(settings.SHKEEPER_MIN_AMOUNT_KOPEKS)}')
        return

    if amount_kopeks > settings.SHKEEPER_MAX_AMOUNT_KOPEKS:
        await message.answer(
            f'Максимальная сумма пополнения: {settings.format_price(settings.SHKEEPER_MAX_AMOUNT_KOPEKS)}'
        )
        return

    payment_service = PaymentService(message.bot)
    result = await payment_service.create_shkeeper_payment(
        db=db,
        user_id=db_user.id,
        amount_kopeks=amount_kopeks,
        description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
    )

    if not result or not result.get('payment_url'):
        await message.answer('❌ Ошибка создания платежа SHKeeper. Попробуйте позже.')
        await state.clear()
        return

    local_payment_id = result['local_payment_id']
    payment_url = result['payment_url']
    invoice_id = result.get('invoice_id') or result.get('order_id')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='💳 Оплатить через SHKeeper', url=payment_url)],
            [types.InlineKeyboardButton(text='📊 Проверить статус', callback_data=f'check_shkeeper_{local_payment_id}')],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]
    )

    state_data = await state.get_data()
    prompt_message_id = state_data.get('shkeeper_prompt_message_id')
    prompt_chat_id = state_data.get('shkeeper_prompt_chat_id', message.chat.id)

    try:
        await message.delete()
    except Exception as error:  # pragma: no cover - зависит от прав в чате
        logger.debug('Не удалось удалить сообщение с суммой SHKeeper', error=error)

    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as error:  # pragma: no cover - зависит от прав в чате
            logger.debug('Не удалось удалить prompt сообщение SHKeeper', error=error)

    await message.answer(
        (
            '💳 <b>Оплата через SHKeeper</b>\n\n'
            f'💰 Сумма: {settings.format_price(amount_kopeks)}\n'
            f'🆔 Платеж: {invoice_id}\n\n'
            '1. Нажмите кнопку оплаты\n'
            '2. Оплатите счет в криптовалюте\n'
            '3. Баланс пополнится автоматически'
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.clear()


@error_handler
async def check_shkeeper_payment_status(
    callback: types.CallbackQuery,
    db: AsyncSession,
):
    try:
        local_payment_id = int(callback.data.split('_')[-1])
    except (ValueError, IndexError):
        await callback.answer('❌ Некорректный идентификатор платежа', show_alert=True)
        return

    payment_service = PaymentService(callback.bot)
    status_info = await payment_service.get_shkeeper_payment_status(db, local_payment_id)
    if not status_info:
        await callback.answer('❌ Платеж не найден', show_alert=True)
        return

    payment = status_info['payment']
    user_language = 'ru'
    try:
        user = await fetch_user_by_id(db, payment.user_id)
        if user and getattr(user, 'language', None):
            user_language = user.language
    except Exception:
        pass

    texts = get_texts(user_language)
    status = (payment.status or 'unknown').lower()
    status_emoji = '✅' if payment.is_paid else ('⏳' if status in {'new', 'pending', 'processing'} else '❌')

    await callback.message.answer(
        (
            f'💳 <b>Статус платежа {settings.get_shkeeper_display_name()}</b>\n\n'
            f'🆔 ID: {payment.shkeeper_invoice_id or payment.order_id}\n'
            f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
            f'📊 Статус: {status_emoji} {payment.status}\n'
            f'📅 Создан: {payment.created_at.strftime("%d.%m.%Y %H:%M") if payment.created_at else "—"}'
        ),
        parse_mode='HTML',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')]]
        ),
    )
    await callback.answer()
