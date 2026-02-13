import logging

from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import get_user_transactions
from app.database.models import TransactionType, User
from app.keyboards.inline import (
    get_back_keyboard,
    get_balance_keyboard,
    get_pagination_keyboard,
    get_payment_methods_keyboard,
)
from app.localization.texts import get_texts
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.price_display import calculate_user_price


logger = logging.getLogger(__name__)

TRANSACTIONS_PER_PAGE = 10


async def route_payment_by_method(
    message: types.Message, db_user: User, amount_kopeks: int, state: FSMContext, payment_method: str
) -> bool:
    """
    Роутер платежей по методу оплаты.

    Args:
        message: Сообщение для ответа
        db_user: Пользователь БД
        amount_kopeks: Сумма в копейках
        state: FSM состояние
        payment_method: Метод оплаты (yookassa, stars, cryptobot и т.д.)

    Returns:
        True если платеж обработан, False если метод неизвестен
    """
    if payment_method == 'stars':
        from .stars import process_stars_payment_amount

        await process_stars_payment_amount(message, db_user, amount_kopeks, state)
        return True

    # Все остальные методы требуют сессию БД
    from app.database.database import AsyncSessionLocal

    if payment_method == 'yookassa':
        from .yookassa import process_yookassa_payment_amount

        async with AsyncSessionLocal() as db:
            await process_yookassa_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'yookassa_sbp':
        from .yookassa import process_yookassa_sbp_payment_amount

        async with AsyncSessionLocal() as db:
            await process_yookassa_sbp_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'mulenpay':
        from .mulenpay import process_mulenpay_payment_amount

        async with AsyncSessionLocal() as db:
            await process_mulenpay_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'platega':
        from .platega import process_platega_payment_amount

        async with AsyncSessionLocal() as db:
            await process_platega_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'wata':
        from .wata import process_wata_payment_amount

        async with AsyncSessionLocal() as db:
            await process_wata_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'pal24':
        from .pal24 import process_pal24_payment_amount

        async with AsyncSessionLocal() as db:
            await process_pal24_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'cryptobot':
        from .cryptobot import process_cryptobot_payment_amount

        async with AsyncSessionLocal() as db:
            await process_cryptobot_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'heleket':
        from .heleket import process_heleket_payment_amount

        async with AsyncSessionLocal() as db:
            await process_heleket_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'cloudpayments':
        from .cloudpayments import process_cloudpayments_payment_amount

        async with AsyncSessionLocal() as db:
            await process_cloudpayments_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'freekassa':
        from .freekassa import process_freekassa_payment_amount

        async with AsyncSessionLocal() as db:
            await process_freekassa_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'kassa_ai':
        from .kassa_ai import process_kassa_ai_payment_amount

        async with AsyncSessionLocal() as db:
            await process_kassa_ai_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    return False


async def get_quick_amount_buttons(language: str, user: User) -> list:
    """
    Generate quick amount buttons with user-specific pricing and discounts.

    Args:
        language: User's language for formatting
        user: User object to calculate personalized discounts

    Returns:
        List of button rows for inline keyboard
    """
    if not settings.is_quick_amount_buttons_enabled():
        return []

    from app.config import PERIOD_PRICES
    from app.localization.texts import get_texts

    texts = get_texts(language)

    # В режиме тарифов получаем цены из тарифа пользователя
    tariff_prices = None
    tariff_periods = None
    if settings.is_tariffs_mode():
        from app.database.crud.subscription import get_subscription_by_user_id
        from app.database.crud.tariff import get_tariff_by_id
        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            subscription = await get_subscription_by_user_id(db, user.id)
            if subscription and subscription.tariff_id:
                tariff = await get_tariff_by_id(db, subscription.tariff_id)
                if tariff and tariff.period_prices:
                    tariff_prices = {int(k): v for k, v in tariff.period_prices.items()}
                    tariff_periods = sorted(tariff_prices.keys())

    buttons = []

    # Используем периоды тарифа в режиме тарифов, иначе стандартные
    if tariff_periods:
        periods = tariff_periods[:6]
    else:
        periods = settings.get_available_subscription_periods()[:6]

    for period in periods:
        # Получаем цену из тарифа или из PERIOD_PRICES
        if tariff_prices and period in tariff_prices:
            base_price_kopeks = tariff_prices[period]
        else:
            base_price_kopeks = PERIOD_PRICES.get(period, 0)

        if base_price_kopeks > 0:
            # Calculate price with user's promo group discount using unified system
            price_info = calculate_user_price(user, base_price_kopeks, period, 'period')

            callback_data = f'quick_amount_{price_info.final_price}'

            # Format button text with discount display
            period_label = texts.t('BALANCE_PERIOD_DAYS_LABEL').format(days=period)

            # For balance buttons, use simpler format without emoji and period label prefix
            if price_info.has_discount:
                button_text = (
                    f'{texts.format_price(price_info.base_price)} ➜ '
                    f'{texts.format_price(price_info.final_price)} '
                    f'(-{price_info.discount_percent}%) • {period_label}'
                )
            else:
                button_text = f'{texts.format_price(price_info.final_price)} • {period_label}'

            buttons.append(types.InlineKeyboardButton(text=button_text, callback_data=callback_data))

    keyboard_rows = []
    for i in range(0, len(buttons), 2):
        keyboard_rows.append(buttons[i : i + 2])

    return keyboard_rows


@error_handler
async def show_balance_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, доступно ли сообщение
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)

    balance_text = texts.BALANCE_INFO.format(balance=texts.format_price(db_user.balance_kopeks))

    reply_markup = get_balance_keyboard(db_user.language)

    try:
        if callback.message and callback.message.text:
            await callback.message.edit_text(balance_text, reply_markup=reply_markup)
        elif callback.message and callback.message.caption:
            await callback.message.edit_caption(balance_text, reply_markup=reply_markup)
        else:
            await callback.message.answer(balance_text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        logger.warning(
            'Failed to edit balance message, sending a new one instead: %s',
            error,
        )
        await callback.message.answer(balance_text, reply_markup=reply_markup)
    await callback.answer()


@error_handler
async def show_balance_history(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)

    offset = (page - 1) * TRANSACTIONS_PER_PAGE

    raw_transactions = await get_user_transactions(db, db_user.id, limit=TRANSACTIONS_PER_PAGE * 3, offset=offset)

    seen_transactions = set()
    unique_transactions = []

    for transaction in raw_transactions:
        rounded_time = transaction.created_at.replace(second=0, microsecond=0)
        transaction_key = (transaction.amount_kopeks, transaction.description, rounded_time)

        if transaction_key not in seen_transactions:
            seen_transactions.add(transaction_key)
            unique_transactions.append(transaction)

            if len(unique_transactions) >= TRANSACTIONS_PER_PAGE:
                break

    all_transactions = await get_user_transactions(db, db_user.id, limit=1000)
    seen_all = set()
    total_unique = 0

    for transaction in all_transactions:
        rounded_time = transaction.created_at.replace(second=0, microsecond=0)
        transaction_key = (transaction.amount_kopeks, transaction.description, rounded_time)
        if transaction_key not in seen_all:
            seen_all.add(transaction_key)
            total_unique += 1

    if not unique_transactions:
        await callback.message.edit_text(
            texts.t('BALANCE_HISTORY_EMPTY'), reply_markup=get_back_keyboard(db_user.language)
        )
        await callback.answer()
        return

    text = texts.t('BALANCE_HISTORY_TITLE')

    for transaction in unique_transactions:
        emoji = '💰' if transaction.type == TransactionType.DEPOSIT.value else '💸'
        amount_text = (
            f'+{texts.format_price(transaction.amount_kopeks)}'
            if transaction.type == TransactionType.DEPOSIT.value
            else f'-{texts.format_price(transaction.amount_kopeks)}'
        )

        text += f'{emoji} {amount_text}\n'
        text += f'📝 {transaction.description}\n'
        text += f'📅 {transaction.created_at.strftime("%d.%m.%Y %H:%M")}\n\n'

    keyboard = []
    total_pages = (total_unique + TRANSACTIONS_PER_PAGE - 1) // TRANSACTIONS_PER_PAGE

    if total_pages > 1:
        pagination_row = get_pagination_keyboard(page, total_pages, 'balance_history', db_user.language)
        keyboard.extend(pagination_row)

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

    await callback.message.edit_text(
        text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode='HTML'
    )
    await callback.answer()


@error_handler
async def handle_balance_history_pagination(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    page = int(callback.data.split('_')[-1])
    await show_balance_history(callback, db_user, db, page)


@error_handler
async def show_payment_methods(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    from app.config import settings
    from app.database.crud.subscription import get_subscription_by_user_id
    from app.services.subscription_service import SubscriptionService
    from app.utils.payment_utils import get_payment_methods_text
    from app.utils.pricing_utils import apply_percentage_discount, calculate_months_from_days

    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or texts.t('PURCHASE_RESTRICTION_DEFAULT_REASON')
        support_url = settings.get_support_contact_url()
        keyboard = []
        if support_url:
            keyboard.append(
                [types.InlineKeyboardButton(text=texts.t('USER_RESTRICTION_APPEAL_BUTTON'), url=support_url)]
            )
        keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

        await callback.message.edit_text(
            texts.t('USER_RESTRICTION_TOPUP_BLOCKED').format(reason=reason),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        )
        await callback.answer()
        return

    payment_text = get_payment_methods_text(db_user.language)

    # Добавляем информацию о текущем тарифе пользователя
    subscription = await get_subscription_by_user_id(db, db_user.id)
    tariff_info = ''
    if subscription and not subscription.is_trial:
        # Рассчитываем приблизительную стоимость продления на 30 дней
        duration_days = 30  # Берем для примера 30 дней
        current_traffic = subscription.traffic_limit_gb
        current_connected_squads = subscription.connected_squads or []
        current_device_limit = subscription.device_limit or settings.DEFAULT_DEVICE_LIMIT

        try:
            # Получаем цены для текущих параметров
            from app.config import PERIOD_PRICES
            from app.database.crud.tariff import get_tariff_by_id

            # В режиме тарифов берём цену из тарифа пользователя
            base_price_original = 0
            if settings.is_tariffs_mode() and subscription.tariff_id:
                tariff = await get_tariff_by_id(db, subscription.tariff_id)
                if tariff and tariff.period_prices:
                    base_price_original = tariff.period_prices.get(str(duration_days), 0)

            # Если не нашли в тарифе - используем PERIOD_PRICES
            if base_price_original <= 0:
                base_price_original = PERIOD_PRICES.get(duration_days, 0)

            period_discount_percent = db_user.get_promo_discount('period', duration_days)
            base_price, base_discount_total = apply_percentage_discount(
                base_price_original,
                period_discount_percent,
            )

            # Рассчитываем стоимость серверов
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            (
                servers_price_per_month,
                per_server_monthly_prices,
            ) = await subscription_service.get_countries_price_by_uuids(
                current_connected_squads,
                db,
                promo_group_id=db_user.promo_group_id,
            )
            servers_discount_percent = db_user.get_promo_discount('servers', duration_days)
            total_servers_price = 0
            for server_price in per_server_monthly_prices:
                discounted_per_month, discount_per_month = apply_percentage_discount(
                    server_price,
                    servers_discount_percent,
                )
                total_servers_price += discounted_per_month

            # Рассчитываем стоимость трафика
            traffic_price_per_month = settings.get_traffic_price(current_traffic)
            traffic_discount_percent = db_user.get_promo_discount('traffic', duration_days)
            traffic_discounted_per_month, traffic_discount_per_month = apply_percentage_discount(
                traffic_price_per_month,
                traffic_discount_percent,
            )

            # Рассчитываем стоимость устройств
            additional_devices = max(0, (current_device_limit or 0) - settings.DEFAULT_DEVICE_LIMIT)
            devices_price_per_month = additional_devices * settings.PRICE_PER_DEVICE
            devices_discount_percent = db_user.get_promo_discount('devices', duration_days)
            devices_discounted_per_month, devices_discount_per_month = apply_percentage_discount(
                devices_price_per_month,
                devices_discount_percent,
            )

            # Общая стоимость
            months_in_period = calculate_months_from_days(duration_days)
            total_price = (
                base_price
                + total_servers_price * months_in_period
                + traffic_discounted_per_month * months_in_period
                + devices_discounted_per_month * months_in_period
            )

            traffic_value = current_traffic or 0
            if traffic_value <= 0:
                traffic_display = texts.t('TRAFFIC_UNLIMITED_SHORT')
            else:
                traffic_display = texts.format_traffic(traffic_value)

            current_tariff_desc = texts.t('BALANCE_CURRENT_TARIFF_DESC').format(
                servers_count=len(current_connected_squads),
                traffic=traffic_display,
                devices=current_device_limit,
            )
            estimated_price_info = texts.t('BALANCE_ESTIMATED_RENEWAL_PRICE').format(
                price=texts.format_price(total_price),
                days=duration_days,
            )
            tariff_info = texts.t('BALANCE_CURRENT_TARIFF_BLOCK').format(
                description=current_tariff_desc,
                estimated_price=estimated_price_info,
            )
        except Exception as e:
            logger.warning(f'Не удалось рассчитать стоимость текущей подписки для пользователя {db_user.id}: {e}')
            tariff_info = ''

    full_text = payment_text + tariff_info

    keyboard = get_payment_methods_keyboard(0, db_user.language)

    # Если сообщение недоступно, отправляем новое
    if isinstance(callback.message, InaccessibleMessage):
        await callback.message.answer(full_text, reply_markup=keyboard, parse_mode='HTML')
        await callback.answer()
        return

    try:
        await callback.message.edit_text(full_text, reply_markup=keyboard, parse_mode='HTML')
    except TelegramBadRequest:
        try:
            await callback.message.edit_caption(full_text, reply_markup=keyboard, parse_mode='HTML')
        except TelegramBadRequest:
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass
            await callback.message.answer(full_text, reply_markup=keyboard, parse_mode='HTML')

    await callback.answer()


@error_handler
async def handle_payment_methods_unavailable(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)

    await callback.answer(
        texts.t('PAYMENT_METHODS_UNAVAILABLE_ALERT'),
        show_alert=True,
    )


@error_handler
async def handle_successful_topup_with_cart(user_id: int, amount_kopeks: int, bot, db: AsyncSession):
    from aiogram.fsm.storage.base import StorageKey

    from app.bot import dp
    from app.database.crud.user import get_user_by_id

    user = await get_user_by_id(db, user_id)
    if not user:
        return

    # Email-only users don't have telegram_id - skip Telegram notification
    if not user.telegram_id:
        logger.info(f'Skipping cart notification for email-only user {user_id}')
        return

    storage = dp.storage
    key = StorageKey(bot_id=bot.id, chat_id=user.telegram_id, user_id=user.telegram_id)

    try:
        state_data = await storage.get_data(key)
        current_state = await storage.get_state(key)

        if current_state == 'SubscriptionStates:cart_saved_for_topup' and state_data.get('saved_cart'):
            texts = get_texts(user.language)
            total_price = state_data.get('total_price', 0)

            keyboard = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('RETURN_TO_SUBSCRIPTION_CHECKOUT'), callback_data='return_to_saved_cart'
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.t('MY_BALANCE_BUTTON'), callback_data='menu_balance')],
                    [types.InlineKeyboardButton(text=texts.t('MAIN_MENU_BUTTON'), callback_data='back_to_menu')],
                ]
            )

            success_text = texts.t('BALANCE_TOPUP_CART_SUCCESS_MESSAGE').format(
                topup_amount=texts.format_price(amount_kopeks),
                current_balance=texts.format_price(user.balance_kopeks),
                cart_total=texts.format_price(total_price),
            )

            await bot.send_message(
                chat_id=user.telegram_id, text=success_text, reply_markup=keyboard, parse_mode='HTML'
            )

    except Exception as e:
        logger.error(f'Ошибка обработки успешного пополнения с корзиной: {e}')


@error_handler
async def request_support_topup(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)

    if not settings.is_support_topup_enabled():
        await callback.answer(
            texts.t('SUPPORT_TOPUP_DISABLED'),
            show_alert=True,
        )
        return

    user_id_display = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    support_text = texts.t('SUPPORT_TOPUP_INFO_MESSAGE').format(
        support_contact=settings.get_support_contact_display_html(),
        user_id=user_id_display,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('SUPPORT_TOPUP_CONTACT_BUTTON'),
                    url=settings.get_support_contact_url() or 'https://t.me/',
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')],
        ]
    )

    await callback.message.edit_text(support_text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


@error_handler
async def process_topup_amount(message: types.Message, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    try:
        if not message.text:
            if message.successful_payment:
                logger.info(
                    'Получено сообщение об успешном платеже без текста, обработчик суммы пополнения завершает работу'
                )
                await state.clear()
                return

            await message.answer(texts.INVALID_AMOUNT, reply_markup=get_back_keyboard(db_user.language))
            return

        amount_text = message.text.strip()
        if not amount_text:
            await message.answer(texts.INVALID_AMOUNT, reply_markup=get_back_keyboard(db_user.language))
            return

        amount_rubles = float(amount_text.replace(',', '.'))

        if amount_rubles < 1:
            await message.answer(texts.t('BALANCE_TOPUP_MIN_AMOUNT_ALERT').format(amount='1 ₽'))
            return

        if amount_rubles > 50000:
            await message.answer(texts.t('BALANCE_TOPUP_MAX_AMOUNT_ALERT').format(amount='50,000 ₽'))
            return

        amount_kopeks = int(amount_rubles * 100)
        data = await state.get_data()
        payment_method = data.get('payment_method', 'stars')

        if payment_method in ['yookassa', 'yookassa_sbp']:
            if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS:
                min_rubles = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
                await message.answer(texts.t('BALANCE_YOOKASSA_MIN_AMOUNT_ALERT').format(amount=f'{min_rubles:.0f} ₽'))
                return

            if amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
                max_rubles = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
                amount_text = f'{max_rubles:,.0f} ₽'.replace(',', ' ')
                await message.answer(texts.t('BALANCE_YOOKASSA_MAX_AMOUNT_ALERT').format(amount=amount_text))
                return

        if not await route_payment_by_method(message, db_user, amount_kopeks, state, payment_method):
            await message.answer(texts.t('SIMPLE_SUB_UNKNOWN_PAYMENT_METHOD_ALERT'))

    except ValueError:
        await message.answer(texts.INVALID_AMOUNT, reply_markup=get_back_keyboard(db_user.language))


@error_handler
async def handle_sbp_payment(callback: types.CallbackQuery, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.split('_')[-1])

        from app.database.crud.yookassa import get_yookassa_payment_by_local_id

        payment = await get_yookassa_payment_by_local_id(db, local_payment_id)

        if not payment:
            await callback.answer(texts.t('SIMPLE_SUB_PAYMENT_NOT_FOUND_ALERT'), show_alert=True)
            return

        import json

        metadata = json.loads(payment.metadata_json) if payment.metadata_json else {}
        confirmation_token = metadata.get('confirmation_token')

        if not confirmation_token:
            await callback.answer(texts.t('BALANCE_SBP_CONFIRMATION_TOKEN_NOT_FOUND_ALERT'), show_alert=True)
            return

        await callback.message.answer(
            texts.t('BALANCE_SBP_PAYMENT_INSTRUCTIONS').format(confirmation_token=confirmation_token),
            parse_mode='HTML',
        )

        await callback.answer(texts.t('BALANCE_SBP_PAYMENT_INFO_SENT_ALERT'), show_alert=True)

    except Exception as e:
        logger.error(f'Ошибка обработки embedded платежа СБП: {e}')
        await callback.answer(texts.t('BALANCE_PAYMENT_PROCESSING_ERROR_ALERT'), show_alert=True)


@error_handler
async def handle_quick_amount_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Обработчик выбора суммы через кнопки быстрого выбора
    """
    # Проверяем, что пользователь в правильном состоянии FSM
    current_state = await state.get_state()
    if current_state != BalanceStates.waiting_for_amount:
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('BALANCE_SELECT_PAYMENT_METHOD_FIRST_ALERT'), show_alert=True)
        return

    # Извлекаем сумму из callback_data
    try:
        amount_kopeks = int(callback.data.split('_')[-1])

        # Получаем метод оплаты из состояния
        data = await state.get_data()
        payment_method = data.get('payment_method', 'yookassa')
        texts = get_texts(db_user.language)

        # Роутим платеж на соответствующий обработчик
        if not await route_payment_by_method(callback.message, db_user, amount_kopeks, state, payment_method):
            await callback.answer(texts.t('SIMPLE_SUB_UNKNOWN_PAYMENT_METHOD_ALERT'), show_alert=True)
            return

    except ValueError:
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('BALANCE_AMOUNT_PROCESSING_ERROR_ALERT'), show_alert=True)
    except Exception as e:
        logger.error(f'Ошибка обработки быстрого выбора суммы: {e}')
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('BALANCE_REQUEST_PROCESSING_ERROR_ALERT'), show_alert=True)


@error_handler
async def handle_topup_amount_callback(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    texts = get_texts(db_user.language)
    try:
        _, method, amount_str = callback.data.split('|', 2)
        amount_kopeks = int(amount_str)
    except ValueError:
        await callback.answer(texts.t('BALANCE_INVALID_REQUEST_ALERT'), show_alert=True)
        return

    if amount_kopeks <= 0:
        await callback.answer(texts.t('BALANCE_INVALID_AMOUNT_ALERT'), show_alert=True)
        return

    try:
        # Особые случаи, требующие специальной логики
        if method == 'platega':
            from app.database.database import AsyncSessionLocal

            from .platega import process_platega_payment_amount, start_platega_payment

            data = await state.get_data()
            method_code = int(data.get('platega_method', 0)) if data else 0

            if method_code > 0:
                async with AsyncSessionLocal() as db:
                    await process_platega_payment_amount(callback.message, db_user, db, amount_kopeks, state)
            else:
                await state.update_data(platega_pending_amount=amount_kopeks)
                await start_platega_payment(callback, db_user, state)
        elif method == 'tribute':
            from .tribute import start_tribute_payment

            await start_tribute_payment(callback, db_user)
            return
        # Стандартные методы через роутер
        elif not await route_payment_by_method(callback.message, db_user, amount_kopeks, state, method):
            await callback.answer(texts.t('SIMPLE_SUB_UNKNOWN_PAYMENT_METHOD_ALERT'), show_alert=True)
            return

        await callback.answer()

    except Exception as error:
        logger.error(f'Ошибка быстрого пополнения: {error}')
        await callback.answer(texts.t('BALANCE_REQUEST_PROCESSING_ERROR_ALERT'), show_alert=True)


def register_balance_handlers(dp: Dispatcher):
    dp.callback_query.register(show_balance_menu, F.data == 'menu_balance')

    dp.callback_query.register(show_balance_history, F.data == 'balance_history')

    dp.callback_query.register(handle_balance_history_pagination, F.data.startswith('balance_history_page_'))

    dp.callback_query.register(show_payment_methods, F.data == 'balance_topup')

    from .stars import start_stars_payment

    dp.callback_query.register(start_stars_payment, F.data == 'topup_stars')

    from .yookassa import start_yookassa_payment

    dp.callback_query.register(start_yookassa_payment, F.data == 'topup_yookassa')

    from .yookassa import start_yookassa_sbp_payment

    dp.callback_query.register(start_yookassa_sbp_payment, F.data == 'topup_yookassa_sbp')

    from .mulenpay import start_mulenpay_payment

    dp.callback_query.register(start_mulenpay_payment, F.data == 'topup_mulenpay')

    from .wata import start_wata_payment

    dp.callback_query.register(start_wata_payment, F.data == 'topup_wata')

    from .pal24 import start_pal24_payment

    dp.callback_query.register(start_pal24_payment, F.data == 'topup_pal24')
    from .pal24 import handle_pal24_method_selection

    dp.callback_query.register(
        handle_pal24_method_selection,
        F.data.startswith('pal24_method_'),
    )

    from .platega import handle_platega_method_selection, start_platega_payment

    dp.callback_query.register(start_platega_payment, F.data == 'topup_platega')
    dp.callback_query.register(
        handle_platega_method_selection,
        F.data.startswith('platega_method_'),
    )

    from .yookassa import check_yookassa_payment_status

    dp.callback_query.register(check_yookassa_payment_status, F.data.startswith('check_yookassa_'))

    from .tribute import start_tribute_payment

    dp.callback_query.register(start_tribute_payment, F.data == 'topup_tribute')

    dp.callback_query.register(request_support_topup, F.data == 'topup_support')

    from .yookassa import check_yookassa_payment_status

    dp.callback_query.register(check_yookassa_payment_status, F.data.startswith('check_yookassa_'))

    dp.message.register(process_topup_amount, BalanceStates.waiting_for_amount)

    from .cryptobot import start_cryptobot_payment

    dp.callback_query.register(start_cryptobot_payment, F.data == 'topup_cryptobot')

    from .cryptobot import check_cryptobot_payment_status

    dp.callback_query.register(check_cryptobot_payment_status, F.data.startswith('check_cryptobot_'))

    from .heleket import check_heleket_payment_status, start_heleket_payment

    dp.callback_query.register(start_heleket_payment, F.data == 'topup_heleket')
    dp.callback_query.register(check_heleket_payment_status, F.data.startswith('check_heleket_'))

    from .cloudpayments import handle_cloudpayments_quick_amount, start_cloudpayments_payment

    dp.callback_query.register(start_cloudpayments_payment, F.data == 'topup_cloudpayments')
    dp.callback_query.register(handle_cloudpayments_quick_amount, F.data.startswith('topup_amount|cloudpayments|'))

    from .freekassa import process_freekassa_quick_amount, start_freekassa_topup

    dp.callback_query.register(start_freekassa_topup, F.data == 'topup_freekassa')
    dp.callback_query.register(process_freekassa_quick_amount, F.data.startswith('topup_amount|freekassa|'))

    from .kassa_ai import process_kassa_ai_quick_amount, start_kassa_ai_topup

    dp.callback_query.register(start_kassa_ai_topup, F.data == 'topup_kassa_ai')
    dp.callback_query.register(process_kassa_ai_quick_amount, F.data.startswith('topup_amount|kassa_ai|'))

    from .mulenpay import check_mulenpay_payment_status

    dp.callback_query.register(check_mulenpay_payment_status, F.data.startswith('check_mulenpay_'))

    from .wata import check_wata_payment_status

    dp.callback_query.register(check_wata_payment_status, F.data.startswith('check_wata_'))

    from .pal24 import check_pal24_payment_status

    dp.callback_query.register(check_pal24_payment_status, F.data.startswith('check_pal24_'))

    from .platega import check_platega_payment_status

    dp.callback_query.register(check_platega_payment_status, F.data.startswith('check_platega_'))

    dp.callback_query.register(handle_payment_methods_unavailable, F.data == 'payment_methods_unavailable')

    # Регистрируем обработчик для кнопок быстрого выбора суммы
    dp.callback_query.register(handle_quick_amount_selection, F.data.startswith('quick_amount_'))

    dp.callback_query.register(handle_topup_amount_callback, F.data.startswith('topup_amount|'))
