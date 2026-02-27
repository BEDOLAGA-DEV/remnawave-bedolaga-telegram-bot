import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext

from app.config import settings
from app.database.models import User
from app.external.telegram_stars import TelegramStarsService
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_stars_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await callback.answer(
            texts.t(
                'STARS_TOPUP_NOT_AVAILABLE',
                '❌ Пополнение через Stars временно недоступно',
            ),
            show_alert=True,
        )
        return

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or texts.t(
            'USER_RESTRICTION_REASON_DEFAULT', 'Действие ограничено администратором'
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'),
                        url=support_url,
                    )
                ]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            texts.t(
                'USER_RESTRICTION_TOPUP_BLOCKED',
                '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    # Формируем текст сообщения в зависимости от настройки
    if settings.is_quick_amount_buttons_enabled():
        message_text = texts.t(
            'STARS_TOPUP_PROMPT_QUICK',
            '⭐ <b>Пополнение через Telegram Stars</b>\n\nВыберите сумму пополнения или введите вручную:',
        )
    else:
        message_text = texts.TOP_UP_AMOUNT

    # Создаем клавиатуру
    keyboard = get_back_keyboard(db_user.language)

    # Если включен быстрый выбор суммы и не отключены кнопки, добавляем кнопки
    if settings.is_quick_amount_buttons_enabled():
        from .main import get_quick_amount_buttons

        quick_amount_buttons = await get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            # Вставляем кнопки быстрого выбора перед кнопкой "Назад"
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard

    await callback.message.edit_text(message_text, reply_markup=keyboard)

    await state.update_data(
        stars_prompt_message_id=callback.message.message_id,
        stars_prompt_chat_id=callback.message.chat.id,
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(payment_method='stars')
    await callback.answer()


@error_handler
async def process_stars_payment_amount(message: types.Message, db_user: User, amount_kopeks: int, state: FSMContext):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or texts.t(
            'USER_RESTRICTION_REASON_DEFAULT', 'Действие ограничено администратором'
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'),
                        url=support_url,
                    )
                ]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await message.answer(
            texts.t(
                'USER_RESTRICTION_TOPUP_BLOCKED',
                '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        await state.clear()
        return

    texts = get_texts(db_user.language)

    if not settings.TELEGRAM_STARS_ENABLED:
        await message.answer(
            texts.t(
                'STARS_TOPUP_NOT_AVAILABLE',
                '❌ Пополнение через Stars временно недоступно',
            )
        )
        return

    try:
        amount_rubles = amount_kopeks / 100
        stars_amount = TelegramStarsService.calculate_stars_from_rubles(amount_rubles)
        stars_rate = settings.get_stars_rate()

        payment_service = PaymentService(message.bot)
        invoice_link = await payment_service.create_stars_invoice(
            amount_kopeks=amount_kopeks,
            description=texts.t(
                'STARS_PAYMENT_DESCRIPTION_TOPUP',
                'Пополнение баланса на {amount}',
            ).format(amount=texts.format_price(amount_kopeks)),
            payload=f'balance_{db_user.id}_{amount_kopeks}',
        )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('STARS_PAY_BUTTON', '⭐ Оплатить'), url=invoice_link)],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        state_data = await state.get_data()

        prompt_message_id = state_data.get('stars_prompt_message_id')
        prompt_chat_id = state_data.get('stars_prompt_chat_id', message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - зависит от прав бота
            logger.warning('Не удалось удалить сообщение с суммой Stars', delete_error=delete_error)

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - диагностический лог
                logger.warning('Не удалось удалить сообщение с запросом суммы Stars', delete_error=delete_error)

        invoice_message = await message.answer(
            texts.t(
                'STARS_PAYMENT_INVOICE_MESSAGE',
                '⭐ <b>Оплата через Telegram Stars</b>\n\n'
                '💰 Сумма: {amount}\n'
                '⭐ К оплате: {stars_amount} звезд\n'
                '📊 Курс: {stars_rate}₽ за звезду\n\n'
                'Нажмите кнопку ниже для оплаты:',
            ).format(
                amount=texts.format_price(amount_kopeks),
                stars_amount=stars_amount,
                stars_rate=stars_rate,
            ),
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        await state.update_data(
            stars_invoice_message_id=invoice_message.message_id,
            stars_invoice_chat_id=invoice_message.chat.id,
        )

        await state.set_state(None)

    except Exception as e:
        logger.error('Ошибка создания Stars invoice', error=e)
        await message.answer(
            texts.t('STARS_PAYMENT_CREATE_ERROR', '⚠️ Ошибка создания платежа')
        )
