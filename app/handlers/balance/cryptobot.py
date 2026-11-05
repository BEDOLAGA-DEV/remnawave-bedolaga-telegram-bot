import html
import math

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.timezone import format_local_datetime
from app.utils.topup_retry import format_topup_amount_retry


logger = structlog.get_logger(__name__)


@error_handler
async def start_cryptobot_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('USER_RESTRICTION_DEFAULT_REASON', 'Действие ограничено администратором')
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'), url=support_url
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
            texts.t('PAYMENT_UI_CRYPTO_UNAVAILABLE', '❌ Оплата криптовалютой временно недоступна'), show_alert=True
        )
        return

    from app.utils.currency_converter import currency_converter

    try:
        current_rate = await currency_converter.get_usd_to_rub_rate()
        rate_text = texts.t('PAYMENT_UI_EXCHANGE_RATE_EXACT', '💱 Текущий курс: 1 USD = {value1} ₽').format(
            value1=format(current_rate, '.2f')
        )
    except Exception as e:
        logger.warning('Не удалось получить курс валют', error=e)
        current_rate = 95.0
        rate_text = texts.t('PAYMENT_UI_EXCHANGE_RATE_APPROX', '💱 Курс: 1 USD ≈ {value1} ₽').format(
            value1=format(current_rate, '.0f')
        )

    available_assets = settings.get_cryptobot_assets()
    assets_text = ', '.join(available_assets)

    message_text = texts.t(
        'PAYMENT_UI_CRYPTO_PROMPT',
        '🪙 <b>Пополнение криптовалютой</b>\n\nВведите сумму для пополнения от 100 до 100,000 ₽:\n\n💰 Доступные активы: {value1}\n⚡ Мгновенное зачисление на баланс\n🔒 Безопасная оплата через CryptoBot\n\n{value2}\nСумма будет автоматически конвертирована в USD для оплаты.',
    ).format(value1=assets_text, value2=rate_text)

    keyboard = await get_topup_amount_keyboard('cryptobot', db_user.language, back_callback='back_to_menu')

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
        reason = html.escape(
            getattr(db_user, 'restriction_reason', None)
            or texts.t('USER_RESTRICTION_DEFAULT_REASON', 'Действие ограничено администратором')
        )
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать'), url=support_url
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
        await message.answer(texts.t('PAYMENT_UI_CRYPTO_UNAVAILABLE', '❌ Оплата криптовалютой временно недоступна'))
        return

    amount_rubles = amount_kopeks / 100

    if amount_rubles < 100:
        await message.answer(
            format_topup_amount_retry(db_user.language, 10000, 10000000, too_low=True),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
        )
        return

    if amount_rubles > 100000:
        await message.answer(
            format_topup_amount_retry(db_user.language, 10000, 10000000, too_low=False),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
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
                format_topup_amount_retry(
                    db_user.language,
                    max(10000, math.ceil(current_rate * 100)),
                    min(10000000, math.floor(current_rate * 100000)),
                    too_low=True,
                ),
                reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            )
            return

        if amount_usd > 1000:
            await message.answer(
                format_topup_amount_retry(
                    db_user.language,
                    max(10000, math.ceil(current_rate * 100)),
                    min(10000000, math.floor(current_rate * 100000)),
                    too_low=False,
                ),
                reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
            )
            return

        payment_service = PaymentService(message.bot)

        payment_result = await payment_service.create_cryptobot_payment(
            db=db,
            user_id=db_user.id,
            amount_usd=amount_usd,
            asset=settings.CRYPTOBOT_DEFAULT_ASSET,
            description=f'Пополнение баланса на {amount_rubles:.0f} ₽ ({amount_usd:.2f} USD)',
            payload=f'balance_{db_user.id}_{amount_kopeks}',
        )

        if not payment_result:
            await message.answer(
                texts.t(
                    'PAYMENT_UI_CREATE_ERROR_SUPPORT',
                    '❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.',
                )
            )
            await state.clear()
            return

        bot_invoice_url = payment_result.get('bot_invoice_url')
        mini_app_invoice_url = payment_result.get('mini_app_invoice_url')

        payment_url = bot_invoice_url or mini_app_invoice_url

        if not payment_url:
            await message.answer(
                texts.t(
                    'PAYMENT_UI_LINK_ERROR_SUPPORT', '❌ Ошибка получения ссылки для оплаты. Обратитесь в поддержку.'
                )
            )
            await state.clear()
            return

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('PAY_WITH_COINS_BUTTON', '🪙 Оплатить'), url=payment_url)],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('SUBSCRIPTION_UI_CHECK_STATUS', '📊 Проверить статус'),
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

        if prompt_message_id and prompt_message_id != message.message_id:
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_message_id)
            except Exception as delete_error:  # pragma: no cover - diagnostics
                logger.warning('Не удалось удалить сообщение с запросом суммы CryptoBot', delete_error=delete_error)

        invoice_message = await message.answer(
            texts.t(
                'PAYMENT_UI_CRYPTO_INSTRUCTIONS',
                "🪙 <b>Оплата криптовалютой</b>\n\n💰 Сумма к зачислению: {value1} ₽\n💵 К оплате: {value2} USD\n🪙 Актив: {value3}\n💱 Курс: 1 USD = {value4} ₽\n🆔 ID платежа: {value5}...\n\n📱 <b>Инструкция:</b>\n1. Нажмите кнопку 'Оплатить'\n2. Выберите удобный актив\n3. Переведите указанную сумму\n4. Деньги поступят на баланс автоматически\n\n🔒 Оплата проходит через защищенную систему CryptoBot\n⚡ Поддерживаемые активы: USDT, TON, BTC, ETH\n\n❓ Если возникнут проблемы, обратитесь в {value6}",
            ).format(
                value1=format(amount_rubles, '.0f'),
                value2=format(amount_usd, '.2f'),
                value3=payment_result['asset'],
                value4=format(current_rate, '.2f'),
                value5=payment_result['invoice_id'][:8],
                value6=settings.get_support_contact_display_html(),
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
            'Создан CryptoBot платеж',
            telegram_id=db_user.telegram_id,
            amount_rubles=round(amount_rubles, 0),
            amount_usd=round(amount_usd, 2),
            payment_result=payment_result['invoice_id'],
        )

    except Exception as e:
        logger.error('Ошибка создания CryptoBot платежа', error=e)
        await message.answer(
            texts.t(
                'PAYMENT_UI_CREATE_ERROR_SUPPORT',
                '❌ Ошибка создания платежа. Попробуйте позже или обратитесь в поддержку.',
            )
        )
        await state.clear()


@error_handler
async def check_cryptobot_payment_status(callback: types.CallbackQuery, db: AsyncSession, db_user: User | None = None):
    texts = get_texts(
        getattr(db_user, 'language', None)
        or getattr(getattr(callback, 'from_user', None), 'language_code', None)
        or DEFAULT_LANGUAGE
    )
    if db_user is None:
        await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
        return
    try:
        local_payment_id = int(callback.data.split('_')[-1])

        from app.database.crud.cryptobot import get_cryptobot_payment_by_id

        payment = await get_cryptobot_payment_by_id(db, local_payment_id)

        if not payment or payment.user_id != db_user.id:
            await callback.answer(texts.t('PAYMENT_UI_NOT_FOUND', '❌ Платеж не найден'), show_alert=True)
            return

        status_emoji = {'active': '⏳', 'paid': '✅', 'expired': '❌'}

        status_text = {
            'active': texts.t('WATA_STATUS_OPENED', 'Ожидает оплаты'),
            'paid': texts.t('WATA_STATUS_PAID', 'Оплачен'),
            'expired': texts.t('PAYMENT_UI_STATUS_EXPIRED', 'Истек'),
        }

        emoji = status_emoji.get(payment.status, '❓')
        status = status_text.get(payment.status, texts.t('WATA_STATUS_UNKNOWN', 'Неизвестно'))

        message_text = texts.t(
            'PAYMENT_UI_CRYPTO_STATUS',
            '🪙 Статус платежа:\n\n🆔 ID: {value1}...\n💰 Сумма: {value2} {value3}\n📊 Статус: {value4} {value5}\n📅 Создан: {value6}\n',
        ).format(
            value1=payment.invoice_id[:8],
            value2=payment.amount,
            value3=payment.asset,
            value4=emoji,
            value5=status,
            value6=format_local_datetime(payment.created_at, '%d.%m.%Y %H:%M'),
        )

        if payment.is_paid:
            message_text += texts.t(
                'PAYMENT_UI_SUCCESS_CREDITED', '\n✅ Платеж успешно завершен!\n\nСредства зачислены на баланс.'
            )
        elif payment.is_pending:
            message_text += texts.t(
                'PAYMENT_UI_WAITING_PAY_BUTTON', "\n⏳ Платеж ожидает оплаты. Нажмите кнопку 'Оплатить' выше."
            )
        elif payment.is_expired:
            message_text += texts.t('PAYMENT_UI_EXPIRED_SUPPORT', '\n❌ Платеж истек. Обратитесь в {value1}').format(
                value1=settings.get_support_contact_display()
            )

        await callback.answer(message_text, show_alert=True)

    except Exception as e:
        logger.error('Ошибка проверки статуса CryptoBot платежа', error=e)
        await callback.answer(texts.t('PAYMENT_UI_STATUS_ERROR', '❌ Ошибка проверки статуса'), show_alert=True)
