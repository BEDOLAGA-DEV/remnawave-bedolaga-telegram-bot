import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def start_cryptobot_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
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

        await callback.message.edit_text(
            texts.t(
                'USER_RESTRICTION_TOPUP_BLOCKED',
                '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
            ).format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    if not settings.is_cryptobot_enabled():
        await callback.answer(
            texts.t('CRYPTOBOT_NOT_AVAILABLE', '❌ Оплата криптовалютой временно недоступна'),
            show_alert=True,
        )
        return

    from app.utils.currency_converter import currency_converter

    try:
        current_rate = await currency_converter.get_usd_to_rub_rate()
        rate_text = texts.t(
            'CRYPTOBOT_CURRENT_RATE',
            '💱 Текущий курс: 1 USD = {rate:.2f} ₽',
        ).format(rate=current_rate)
    except Exception as e:
        logger.warning('Не удалось получить курс валют', error=e)
        current_rate = 95.0
        rate_text = texts.t(
            'CRYPTOBOT_FALLBACK_RATE',
            '💱 Курс: 1 USD ≈ {rate:.0f} ₽',
        ).format(rate=current_rate)

    available_assets = settings.get_cryptobot_assets()
    assets_text = ', '.join(available_assets)

    # Формируем текст сообщения в зависимости от настройки
    if settings.is_quick_amount_buttons_enabled():
        message_text = texts.t(
            'CRYPTOBOT_TOPUP_PROMPT_QUICK',
            '🪙 <b>Пополнение криптовалютой</b>\n\n'
            'Выберите сумму пополнения или введите вручную сумму от 100 до 100,000 ₽:\n\n'
            '💰 Доступные активы: {assets}\n'
            '⚡ Мгновенное зачисление на баланс\n'
            '🔒 Безопасная оплата через CryptoBot\n\n'
            '{rate_text}\n'
            'Сумма будет автоматически конвертирована в USD для оплаты.',
        ).format(assets=assets_text, rate_text=rate_text)
    else:
        message_text = texts.t(
            'CRYPTOBOT_TOPUP_PROMPT_MANUAL',
            '🪙 <b>Пополнение криптовалютой</b>\n\n'
            'Введите сумму для пополнения от 100 до 100,000 ₽:\n\n'
            '💰 Доступные активы: {assets}\n'
            '⚡ Мгновенное зачисление на баланс\n'
            '🔒 Безопасная оплата через CryptoBot\n\n'
            '{rate_text}\n'
            'Сумма будет автоматически конвертирована в USD для оплаты.',
        ).format(assets=assets_text, rate_text=rate_text)

    # Создаем клавиатуру
    keyboard = get_back_keyboard(db_user.language)

    # Если включен быстрый выбор суммы и не отключены кнопки, добавляем кнопки
    if settings.is_quick_amount_buttons_enabled():
        from .main import get_quick_amount_buttons

        quick_amount_buttons = await get_quick_amount_buttons(db_user.language, db_user)
        if quick_amount_buttons:
            # Вставляем кнопки быстрого выбора перед кнопкой "Назад"
            keyboard.inline_keyboard = quick_amount_buttons + keyboard.inline_keyboard

    await callback.message.edit_text(message_text, reply_markup=keyboard, parse_mode='HTML')

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='cryptobot',
        current_rate=current_rate,
        cryptobot_prompt_message_id=callback.message.message_id,
        cryptobot_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_cryptobot_payment_amount(
    message: types.Message, db_user: User, db: AsyncSession, amount_kopeks: int, state: FSMContext
):
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

    if not settings.is_cryptobot_enabled():
        await message.answer(
            texts.t('CRYPTOBOT_NOT_AVAILABLE', '❌ Оплата криптовалютой временно недоступна'),
        )
        return

    amount_rubles = amount_kopeks / 100

    if amount_rubles < 100:
        await message.answer(
            texts.t(
                'AMOUNT_TOO_LOW',
                'Минимальная сумма пополнения: {min_amount:.0f}₽',
            ).format(min_amount=100),
        )
        return

    if amount_rubles > 100000:
        await message.answer(
            texts.t(
                'AMOUNT_TOO_HIGH',
                'Максимальная сумма пополнения: {max_amount:,.0f}₽',
            ).format(max_amount=100000),
        )
        return

    try:
        data = await state.get_data()
        current_rate = data.get('current_rate')

        if not current_rate:
            from app.utils.currency_converter import currency_converter

            current_rate = await currency_converter.get_usd_to_rub_rate()

        amount_usd = amount_rubles / current_rate

        amount_usd = round(amount_usd, 2)

        if amount_usd < 1:
            await message.answer(
                texts.t(
                    'CRYPTOBOT_MIN_USD_AMOUNT_ERROR',
                    '❌ Минимальная сумма для оплаты в USD: 1.00 USD',
                ),
            )
            return

        if amount_usd > 1000:
            await message.answer(
                texts.t(
                    'CRYPTOBOT_MAX_USD_AMOUNT_ERROR',
                    '❌ Максимальная сумма для оплаты в USD: 1,000 USD',
                ),
            )
            return

        payment_service = PaymentService(message.bot)

        payment_result = await payment_service.create_cryptobot_payment(
            db=db,
            user_id=db_user.id,
            amount_usd=amount_usd,
            asset=settings.CRYPTOBOT_DEFAULT_ASSET,
            description=texts.t(
                'CRYPTOBOT_INVOICE_DESCRIPTION_TOPUP',
                'Пополнение баланса на {amount_rub:.0f} ₽ ({amount_usd:.2f} USD)',
            ).format(amount_rub=amount_rubles, amount_usd=amount_usd),
            payload=f'balance_{db_user.id}_{amount_kopeks}',
        )

        if not payment_result:
            await message.answer(
                texts.t(
                    'CRYPTOBOT_CREATE_PAYMENT_ERROR',
                    '❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.',
                ),
            )
            await state.clear()
            return

        bot_invoice_url = payment_result.get('bot_invoice_url')
        mini_app_invoice_url = payment_result.get('mini_app_invoice_url')

        payment_url = bot_invoice_url or mini_app_invoice_url

        if not payment_url:
            await message.answer(
                texts.t(
                    'CRYPTOBOT_PAYMENT_LINK_ERROR',
                    '❌ Ошибка получения ссылки для оплаты. Обратитесь в поддержку.',
                ),
            )
            await state.clear()
            return

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('PAY_WITH_COINS_BUTTON', '🪙 Оплатить'), url=payment_url)],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                        callback_data=f'check_cryptobot_{payment_result["local_payment_id"]}',
                    )
                ],
                [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
            ]
        )

        state_data = await state.get_data()
        prompt_message_id = state_data.get('cryptobot_prompt_message_id')
        prompt_chat_id = state_data.get('cryptobot_prompt_chat_id', message.chat.id)

        try:
            await message.delete()
        except Exception as delete_error:  # pragma: no cover - depends on bot rights
            logger.warning('Не удалось удалить сообщение с суммой CryptoBot', delete_error=delete_error)

        if prompt_message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - diagnostics
                logger.warning('Не удалось удалить сообщение с запросом суммы CryptoBot', delete_error=delete_error)

        invoice_message = await message.answer(
            texts.t(
                'CRYPTOBOT_INVOICE_MESSAGE',
                '🪙 <b>Оплата криптовалютой</b>\n\n'
                '💰 Сумма к зачислению: {amount_rub:.0f} ₽\n'
                '💵 К оплате: {amount_usd:.2f} USD\n'
                '🪙 Актив: {asset}\n'
                '💱 Курс: 1 USD = {rate:.2f} ₽\n'
                '🆔 ID платежа: {invoice_short}...\n\n'
                '📱 <b>Инструкция:</b>\n'
                "1. Нажмите кнопку 'Оплатить'\n"
                '2. Выберите удобный актив\n'
                '3. Переведите указанную сумму\n'
                '4. Деньги поступят на баланс автоматически\n\n'
                '🔒 Оплата проходит через защищенную систему CryptoBot\n'
                '⚡ Поддерживаемые активы: USDT, TON, BTC, ETH\n\n'
                '❓ Если возникнут проблемы, обратитесь в {support_contact}',
            ).format(
                amount_rub=amount_rubles,
                amount_usd=amount_usd,
                asset=payment_result['asset'],
                rate=current_rate,
                invoice_short=payment_result['invoice_id'][:8],
                support_contact=settings.get_support_contact_display_html(),
            ),
            reply_markup=keyboard,
            parse_mode='HTML',
        )

        await state.update_data(
            cryptobot_invoice_message_id=invoice_message.message_id,
            cryptobot_invoice_chat_id=invoice_message.chat.id,
        )

        await state.clear()

        logger.info(
            'Создан CryptoBot платеж для пользователя ₽ ( USD), ID',
            telegram_id=db_user.telegram_id,
            amount_rubles=round(amount_rubles, 0),
            amount_usd=round(amount_usd, 2),
            payment_result=payment_result['invoice_id'],
        )

    except Exception as e:
        logger.error('Ошибка создания CryptoBot платежа', error=e)
        await message.answer(
            texts.t(
                'CRYPTOBOT_CREATE_PAYMENT_ERROR',
                '❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.',
            ),
        )
        await state.clear()


@error_handler
async def check_cryptobot_payment_status(callback: types.CallbackQuery, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.split('_')[-1])

        from app.database.crud.cryptobot import get_cryptobot_payment_by_id

        payment = await get_cryptobot_payment_by_id(db, local_payment_id)

        if not payment:
            user = callback.from_user
            language = getattr(user, 'language_code', 'ru') if user else 'ru'
            texts = get_texts(language)
            await callback.answer(
                texts.t('ADMIN_PAYMENT_NOT_FOUND', 'Платёж не найден.'),
                show_alert=True,
            )
            return

        user = callback.from_user
        payment_user = getattr(payment, 'user', None)
        language = getattr(payment_user, 'language', None) or (getattr(user, 'language_code', 'ru') if user else 'ru')
        texts = get_texts(language)

        status_emoji = {'active': '⏳', 'paid': '✅', 'expired': '❌'}

        status_text = {
            'active': texts.t('ADMIN_PAYMENT_STATUS_PENDING', 'Ожидает оплаты'),
            'paid': texts.t('ADMIN_PAYMENT_STATUS_PAID', 'Оплачен'),
            'expired': texts.t('ADMIN_PAYMENT_STATUS_EXPIRED', 'Просрочен'),
        }

        emoji = status_emoji.get(payment.status, '❓')
        status = status_text.get(payment.status, texts.t('SUBSCRIPTION_STATUS_UNKNOWN', 'Неизвестно'))

        message_text = texts.t(
            'CRYPTOBOT_PAYMENT_STATUS_MESSAGE',
            '🪙 Статус платежа:\n\n'
            '🆔 ID: {invoice_short}...\n'
            '💰 Сумма: {amount} {asset}\n'
            '📊 Статус: {emoji} {status}\n'
            '📅 Создан: {created_at}\n',
        ).format(
            invoice_short=payment.invoice_id[:8],
            amount=payment.amount,
            asset=payment.asset,
            emoji=emoji,
            status=status,
            created_at=payment.created_at.strftime('%d.%m.%Y %H:%M'),
        )

        if payment.is_paid:
            message_text += texts.t(
                'CRYPTOBOT_PAYMENT_STATUS_PAID_NOTE',
                '\n✅ Платеж успешно завершен!\n\nСредства зачислены на баланс.',
            )
        elif payment.is_pending:
            message_text += texts.t(
                'CRYPTOBOT_PAYMENT_STATUS_PENDING_NOTE',
                "\n⏳ Платеж ожидает оплаты. Нажмите кнопку 'Оплатить' выше.",
            )
        elif payment.is_expired:
            message_text += texts.t(
                'CRYPTOBOT_PAYMENT_STATUS_EXPIRED_NOTE',
                '\n❌ Платеж истек. Обратитесь в {support_contact}',
            ).format(support_contact=settings.get_support_contact_display())

        await callback.answer(message_text, show_alert=True)

    except Exception as e:
        logger.error('Ошибка проверки статуса CryptoBot платежа', error=e)
        user = callback.from_user
        language = getattr(user, 'language_code', 'ru') if user else 'ru'
        texts = get_texts(language)
        await callback.answer(
            texts.t('CRYPTOBOT_STATUS_CHECK_ERROR', '❌ Ошибка проверки статуса'),
            show_alert=True,
        )
