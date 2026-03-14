import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import get_user_transactions
from app.database.models import TransactionType, User
from app.handlers.subscription.autopay import handle_confirm_unlink, handle_saved_cards_list, handle_unlink_card
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


logger = structlog.get_logger(__name__)

TRANSACTIONS_PER_PAGE = 10

CREDIT_TRANSACTION_TYPES: frozenset[str] = frozenset(
    {
        TransactionType.DEPOSIT.value,
        TransactionType.REFERRAL_REWARD.value,
        TransactionType.REFUND.value,
        TransactionType.POLL_REWARD.value,
    }
)


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

    if payment_method in ('freekassa', 'freekassa_sbp', 'freekassa_card'):
        from .freekassa import process_freekassa_payment_amount

        async with AsyncSessionLocal() as db:
            await process_freekassa_payment_amount(
                message, db_user, db, amount_kopeks, state, payment_method=payment_method
            )
        return True

    if payment_method == 'kassa_ai':
        from .kassa_ai import process_kassa_ai_payment_amount

        async with AsyncSessionLocal() as db:
            await process_kassa_ai_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    if payment_method == 'riopay':
        from .riopay import process_riopay_payment_amount

        async with AsyncSessionLocal() as db:
            await process_riopay_payment_amount(message, db_user, db, amount_kopeks, state)
        return True

    return False


async def get_quick_amount_buttons(language: str, user: User) -> list:
    """
    Generate quick amount buttons with user-specific pricing and discounts.

    Includes full subscription cost: base period price + devices + servers + traffic.

    Args:
        language: User's language for formatting
        user: User object to calculate personalized discounts

    Returns:
        List of button rows for inline keyboard
    """
    if not settings.is_quick_amount_buttons_enabled():
        return []

    from app.config import PERIOD_PRICES
    from app.database.crud.subscription import get_subscription_by_user_id
    from app.database.database import AsyncSessionLocal
    from app.utils.pricing_utils import apply_percentage_discount, calculate_months_from_days

    texts = get_texts(language)

    tariff = None
    tariff_prices = None
    tariff_periods = None
    devices_price_per_month = 0
    servers_per_month_prices: list[int] = []
    traffic_price_per_month = 0

    async with AsyncSessionLocal() as db:
        subscription = await get_subscription_by_user_id(db, user.id)

        # В режиме тарифов получаем цены из тарифа пользователя
        if settings.is_tariffs_mode() and subscription and subscription.tariff_id:
            from app.database.crud.tariff import get_tariff_by_id

            tariff = await get_tariff_by_id(db, subscription.tariff_id)
            if tariff and tariff.period_prices:
                tariff_prices = {int(k): v for k, v in tariff.period_prices.items()}
                tariff_periods = sorted(tariff_prices.keys())

        # Получаем стоимость устройств, серверов и трафика из подписки
        if subscription and not subscription.is_trial:
            # Устройства: в режиме тарифов используем цену и базовый лимит из тарифа
            if settings.is_tariffs_mode() and tariff and tariff_prices:
                tariff_device_price = getattr(tariff, 'device_price_kopeks', None)
                if tariff_device_price and tariff_device_price > 0:
                    device_unit_price = tariff_device_price
                    base_device_limit = tariff.device_limit or 0
                else:
                    device_unit_price = settings.PRICE_PER_DEVICE
                    base_device_limit = settings.DEFAULT_DEVICE_LIMIT
            else:
                device_unit_price = settings.PRICE_PER_DEVICE
                base_device_limit = settings.DEFAULT_DEVICE_LIMIT

            device_limit = subscription.device_limit or base_device_limit
            additional_devices = max(0, device_limit - base_device_limit)
            if additional_devices > 0:
                devices_price_per_month = additional_devices * device_unit_price

            # Серверы
            connected_squads = subscription.connected_squads or []
            if connected_squads:
                from app.services.subscription_service import SubscriptionService

                subscription_service = SubscriptionService()
                _, servers_per_month_prices = await subscription_service.get_countries_price_by_uuids(
                    connected_squads, db, promo_group_id=user.promo_group_id
                )

            # Трафик
            traffic_price_per_month = settings.get_traffic_price(subscription.traffic_limit_gb)

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
            # Базовая цена периода с промо-скидками
            price_info = calculate_user_price(user, base_price_kopeks, period, 'period')

            months = calculate_months_from_days(period)

            # Стоимость устройств со скидкой
            devices_addon = 0
            if devices_price_per_month > 0:
                devices_discount = user.get_promo_discount('devices', period)
                devices_discounted, _ = apply_percentage_discount(devices_price_per_month, devices_discount)
                devices_addon = devices_discounted * months

            # Стоимость серверов со скидкой
            servers_addon = 0
            if servers_per_month_prices:
                servers_discount = user.get_promo_discount('servers', period)
                for server_price in servers_per_month_prices:
                    discounted, _ = apply_percentage_discount(server_price, servers_discount)
                    servers_addon += discounted
                servers_addon *= months

            # Стоимость трафика со скидкой
            traffic_addon = 0
            if traffic_price_per_month > 0:
                traffic_discount = user.get_promo_discount('traffic', period)
                traffic_discounted, _ = apply_percentage_discount(traffic_price_per_month, traffic_discount)
                traffic_addon = traffic_discounted * months

            total_price = price_info.final_price + devices_addon + servers_addon + traffic_addon
            callback_data = f'quick_amount_{total_price}'

            period_label = f'{period} дней'

            # Скидка считается от полной базовой стоимости (период + аддоны без скидок)
            total_base = (
                base_price_kopeks
                + (devices_price_per_month + sum(servers_per_month_prices) + traffic_price_per_month) * months
            )
            has_discount = total_base > total_price and total_base > 0

            if has_discount:
                discount_pct = round((total_base - total_price) * 100 / total_base)
                if discount_pct > 0:
                    button_text = (
                        f'{texts.format_price(total_base)} ➜ '
                        f'{texts.format_price(total_price)} '
                        f'(-{discount_pct}%) • {period_label}'
                    )
                else:
                    button_text = f'{texts.format_price(total_price)} • {period_label}'
            else:
                button_text = f'{texts.format_price(total_price)} • {period_label}'

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
        logger.warning('Failed to edit balance message, sending a new one instead', error=error)
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
        await callback.message.edit_text('📊 История операций пуста', reply_markup=get_back_keyboard(db_user.language))
        await callback.answer()
        return

    text = '📊 <b>История операций</b>\n\n'

    for transaction in unique_transactions:
        is_credit = transaction.type in CREDIT_TRANSACTION_TYPES
        emoji = '💰' if is_credit else '💸'
        amount_text = (
            f'+{texts.format_price(transaction.amount_kopeks)}'
            if is_credit
            else f'-{texts.format_price(abs(transaction.amount_kopeks))}'
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
    from app.utils.payment_utils import get_payment_methods_text

    texts = get_texts(db_user.language)

    # Проверка ограничения на пополнение
    if getattr(db_user, 'restriction_topup', False):
        reason = getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором'
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

    payment_text = get_payment_methods_text(db_user.language)

    full_text = payment_text

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
        texts.t(
            'PAYMENT_METHODS_UNAVAILABLE_ALERT',
            '⚠️ В данный момент автоматические способы оплаты временно недоступны. Для пополнения баланса обратитесь в техподдержку.',
        ),
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
        logger.info('Skipping cart notification for email-only user', user_id=user_id)
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
                            text='🛒 Вернуться к оформлению подписки', callback_data='return_to_saved_cart'
                        )
                    ],
                    [types.InlineKeyboardButton(text='💰 Мой баланс', callback_data='menu_balance')],
                    [types.InlineKeyboardButton(text='🏠 Главное меню', callback_data='back_to_menu')],
                ]
            )

            if 0 < total_price <= user.balance_kopeks:
                balance_hint = 'Средств на балансе достаточно для оформления.'
            else:
                missing = max(total_price - user.balance_kopeks, 0)
                balance_hint = f'Не хватает: {texts.format_price(missing)}'

            success_text = (
                f'✅ Баланс пополнен на {texts.format_price(amount_kopeks)}!\n\n'
                f'💰 Текущий баланс: {texts.format_price(user.balance_kopeks)}\n\n'
                f'🛒 У вас есть сохранённая корзина на {texts.format_price(total_price)}\n'
                f'{balance_hint}\n\n'
                f'Хотите продолжить оформление?'
            )

            await bot.send_message(
                chat_id=user.telegram_id, text=success_text, reply_markup=keyboard, parse_mode='HTML'
            )

    except Exception as e:
        logger.error('Ошибка обработки успешного пополнения с корзиной', error=e)


@error_handler
async def request_support_topup(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)

    if not settings.is_support_topup_enabled():
        await callback.answer(
            texts.t(
                'SUPPORT_TOPUP_DISABLED',
                'Пополнение через поддержку отключено. Попробуйте другой способ оплаты.',
            ),
            show_alert=True,
        )
        return

    user_id_display = db_user.telegram_id or db_user.email or f'#{db_user.id}'
    support_text = f"""
🛠️ <b>Пополнение через поддержку</b>

Для пополнения баланса обратитесь в техподдержку:
{settings.get_support_contact_display_html()}

Укажите:
• ID: {user_id_display}
• Сумму пополнения
• Способ оплаты

⏰ Время обработки: 1-24 часа

<b>Доступные способы:</b>
• Криптовалюта
• Переводы между банками
• Другие платежные системы
"""

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text='💬 Написать в поддержку', url=settings.get_support_contact_url() or 'https://t.me/'
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
            await message.answer('Минимальная сумма пополнения: 1 ₽')
            return

        if amount_rubles > 50000:
            await message.answer('Максимальная сумма пополнения: 50,000 ₽')
            return

        amount_kopeks = int(amount_rubles * 100)
        data = await state.get_data()
        payment_method = data.get('payment_method', 'stars')

        if payment_method in ['yookassa', 'yookassa_sbp']:
            if amount_kopeks < settings.YOOKASSA_MIN_AMOUNT_KOPEKS:
                min_rubles = settings.YOOKASSA_MIN_AMOUNT_KOPEKS / 100
                await message.answer(f'❌ Минимальная сумма для оплаты через YooKassa: {min_rubles:.0f} ₽')
                return

            if amount_kopeks > settings.YOOKASSA_MAX_AMOUNT_KOPEKS:
                max_rubles = settings.YOOKASSA_MAX_AMOUNT_KOPEKS / 100
                await message.answer(
                    f'❌ Максимальная сумма для оплаты через YooKassa: {max_rubles:,.0f} ₽'.replace(',', ' ')
                )
                return

        if not await route_payment_by_method(message, db_user, amount_kopeks, state, payment_method):
            await message.answer('Неизвестный способ оплаты')

    except ValueError:
        await message.answer(texts.INVALID_AMOUNT, reply_markup=get_back_keyboard(db_user.language))


@error_handler
async def handle_sbp_payment(callback: types.CallbackQuery, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.split('_')[-1])

        from app.database.crud.yookassa import get_yookassa_payment_by_local_id

        payment = await get_yookassa_payment_by_local_id(db, local_payment_id)

        if not payment:
            await callback.answer('❌ Платеж не найден', show_alert=True)
            return

        import json

        metadata = json.loads(payment.metadata_json) if payment.metadata_json else {}
        confirmation_token = metadata.get('confirmation_token')

        if not confirmation_token:
            await callback.answer('❌ Токен подтверждения не найден', show_alert=True)
            return

        await callback.message.answer(
            f'Для оплаты через СБП откройте приложение вашего банка и подтвердите платеж.\\n\\n'
            f'Если у вас не открылось банковское приложение автоматически, вы можете:\\n'
            f'1. Скопировать этот токен: <code>{confirmation_token}</code>\\n'
            f'2. Открыть приложение вашего банка\\n'
            f'3. Найти функцию оплаты по токену\\n'
            f'4. Вставить токен и подтвердить платеж',
            parse_mode='HTML',
        )

        await callback.answer('Информация об оплате отправлена', show_alert=True)

    except Exception as e:
        logger.error('Ошибка обработки embedded платежа СБП', error=e)
        await callback.answer('❌ Ошибка обработки платежа', show_alert=True)


@error_handler
async def handle_quick_amount_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """
    Обработчик выбора суммы через кнопки быстрого выбора
    """
    # Проверяем, что пользователь в правильном состоянии FSM
    current_state = await state.get_state()
    if current_state != BalanceStates.waiting_for_amount:
        await callback.answer('❌ Сначала выберите способ оплаты', show_alert=True)
        return

    # Извлекаем сумму из callback_data
    try:
        amount_kopeks = int(callback.data.split('_')[-1])

        # Получаем метод оплаты из состояния
        data = await state.get_data()
        payment_method = data.get('payment_method', 'yookassa')

        # Роутим платеж на соответствующий обработчик
        if not await route_payment_by_method(callback.message, db_user, amount_kopeks, state, payment_method):
            await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)
            return

    except ValueError:
        await callback.answer('❌ Ошибка обработки суммы', show_alert=True)
    except Exception as e:
        logger.error('Ошибка обработки быстрого выбора суммы', error=e)
        await callback.answer('❌ Ошибка обработки запроса', show_alert=True)


@error_handler
async def handle_topup_amount_callback(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
):
    try:
        _, method, amount_str = callback.data.split('|', 2)
        amount_kopeks = int(amount_str)
    except ValueError:
        await callback.answer('❌ Некорректный запрос', show_alert=True)
        return

    if amount_kopeks <= 0:
        await callback.answer('❌ Некорректная сумма', show_alert=True)
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
            await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)
            return

        await callback.answer()

    except Exception as error:
        logger.error('Ошибка быстрого пополнения', error=error)
        await callback.answer('❌ Ошибка обработки запроса', show_alert=True)


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

    from .freekassa import (
        process_freekassa_card_quick_amount,
        process_freekassa_quick_amount,
        process_freekassa_sbp_quick_amount,
        start_freekassa_card_topup,
        start_freekassa_sbp_topup,
        start_freekassa_topup,
    )

    dp.callback_query.register(start_freekassa_topup, F.data == 'topup_freekassa')
    dp.callback_query.register(process_freekassa_quick_amount, F.data.startswith('topup_amount|freekassa|'))
    dp.callback_query.register(start_freekassa_sbp_topup, F.data == 'topup_freekassa_sbp')
    dp.callback_query.register(process_freekassa_sbp_quick_amount, F.data.startswith('topup_amount|freekassa_sbp|'))
    dp.callback_query.register(start_freekassa_card_topup, F.data == 'topup_freekassa_card')
    dp.callback_query.register(process_freekassa_card_quick_amount, F.data.startswith('topup_amount|freekassa_card|'))

    from .kassa_ai import process_kassa_ai_quick_amount, start_kassa_ai_topup

    dp.callback_query.register(start_kassa_ai_topup, F.data == 'topup_kassa_ai')
    dp.callback_query.register(process_kassa_ai_quick_amount, F.data.startswith('topup_amount|kassa_ai|'))

    from .riopay import process_riopay_quick_amount, start_riopay_topup

    dp.callback_query.register(start_riopay_topup, F.data == 'topup_riopay')
    dp.callback_query.register(process_riopay_quick_amount, F.data.startswith('topup_amount|riopay|'))

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

    dp.callback_query.register(handle_saved_cards_list, F.data == 'saved_cards_list')
    dp.callback_query.register(handle_unlink_card, F.data.startswith('unlink_card_'))
    dp.callback_query.register(handle_confirm_unlink, F.data.startswith('confirm_unlink_'))
