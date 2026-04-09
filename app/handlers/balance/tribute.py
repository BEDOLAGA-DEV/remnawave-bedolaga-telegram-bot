import html

import structlog
from aiogram import types

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_tribute_payment(
    callback: types.CallbackQuery,
    db_user: User,
):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\n'
            'Если вы считаете это ошибкой, вы можете обжаловать решение.',
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.TRIBUTE_ENABLED:
        await callback.answer('❌ Оплата картой временно недоступна', show_alert=True)
        return

    try:
        from app.services.tribute_service import TributeService

        tribute_service = TributeService(callback.bot)
        payment_url = await tribute_service.create_payment_link(
            user_id=db_user.telegram_id,
            amount_kopeks=0,
            description='Пополнение баланса VPN',
        )

        if not payment_url:
            await callback.answer('❌ Ошибка создания платежа', show_alert=True)
            return

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='💳 Перейти к оплате', url=payment_url)],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        message_text = (
            '💳 <b>Пополнение банковской картой</b>\n\n'
            '• Введите любую сумму от 100₽\n'
            '• Безопасная оплата через Tribute\n'
            '• Мгновенное зачисление на баланс\n'
            '• Принимаем карты Visa, MasterCard, МИР\n\n'
            '• 🚨 НЕ ОТПРАВЛЯТЬ ПЛАТЕЖ АНОНИМНО!\n\n'
            'Нажмите кнопку для перехода к оплате:'
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        TributeService.remember_invoice_message(
            db_user.telegram_id,
            callback.message.chat.id,
            callback.message.message_id,
        )

    except Exception as e:
        logger.error('Ошибка создания Tribute платежа', error=e)
        await callback.answer('❌ Ошибка создания платежа', show_alert=True)

    await callback.answer()
