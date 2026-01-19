"""
Handlers для создания и активации подарочных подписок.
"""
import logging
from aiogram import Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.states import GiftSubscriptionStates
from app.database.models import User
from app.database.crud.user import get_user_by_id
from app.localization.texts import get_texts
from app.services.gift_subscription_service import (
    gift_subscription_service,
    InsufficientBalanceError,
)
from app.keyboards.gift_keyboards import (
    get_gift_period_keyboard,
    get_gift_traffic_keyboard,
    get_gift_devices_keyboard,
    get_gift_confirm_keyboard,
    get_gift_share_keyboard,
    get_gift_cancel_keyboard,
)
from app.utils.decorators import error_handler
from app.config import settings

logger = logging.getLogger(__name__)


@error_handler
async def start_gift_subscription_flow(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Точка входа в flow создания подарочной подписки.
    """
    texts = get_texts(db_user.language)

    # Очищаем state и начинаем новый flow
    await state.clear()

    # Проверяем режим продаж
    if settings.is_tariffs_mode():
        # Режим тарифов - показываем список тарифов
        await show_gift_tariffs_list(callback, db_user, state, db)
    else:
        # Классический режим - выбор параметров по отдельности
        # Отправляем сообщение с выбором периода
        if isinstance(callback.message, InaccessibleMessage):
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=texts.GIFT_SELECT_PERIOD,
                reply_markup=get_gift_period_keyboard()
            )
        else:
            await callback.message.edit_text(
                text=texts.GIFT_SELECT_PERIOD,
                reply_markup=get_gift_period_keyboard()
            )

        await state.set_state(GiftSubscriptionStates.selecting_period)

    await callback.answer()


@error_handler
async def handle_gift_period_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обработка выбора периода подписки.
    """
    texts = get_texts(db_user.language)

    # Извлекаем период из callback_data (формат: gift_period:30)
    _, period_str = callback.data.split(":")
    period_days = int(period_str)

    # Сохраняем период в state
    await state.update_data(period_days=period_days)

    # Проверяем режим выбора трафика
    if settings.is_traffic_fixed():
        # Трафик фиксированный - используем значение из настроек
        traffic_gb = settings.get_fixed_traffic_limit()
        await state.update_data(traffic_gb=traffic_gb)

        logger.info(f"Gift: Трафик фиксированный ({traffic_gb} ГБ), пропускаем выбор")

        # Проверяем, нужен ли выбор устройств
        if not settings.is_devices_selection_enabled():
            # Устройства тоже фиксированные
            devices = settings.DEVICES_SELECTION_DISABLED_AMOUNT if settings.DEVICES_SELECTION_DISABLED_AMOUNT > 0 else settings.DEFAULT_DEVICE_LIMIT
            await state.update_data(devices=devices)

            # Переходим сразу к выбору серверов или подтверждению
            await _handle_gift_continue_to_servers_or_confirm(callback, db_user, state, db)
        else:
            # Переходим к выбору устройств
            await callback.message.edit_text(
                text=texts.GIFT_SELECT_DEVICES,
                reply_markup=get_gift_devices_keyboard()
            )
            await state.set_state(GiftSubscriptionStates.selecting_devices)
    else:
        # Переходим к выбору трафика
        await callback.message.edit_text(
            text=texts.GIFT_SELECT_TRAFFIC,
            reply_markup=get_gift_traffic_keyboard()
        )
        await state.set_state(GiftSubscriptionStates.selecting_traffic)

    await callback.answer()


@error_handler
async def handle_gift_traffic_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обработка выбора трафика.
    """
    texts = get_texts(db_user.language)

    # Извлекаем трафик из callback_data (формат: gift_traffic:100)
    _, traffic_str = callback.data.split(":")
    traffic_gb = int(traffic_str)

    # Сохраняем трафик в state
    await state.update_data(traffic_gb=traffic_gb)

    # Проверяем, включен ли выбор устройств
    if not settings.is_devices_selection_enabled():
        # Устройства не выбираются - используем значение по умолчанию
        devices = settings.DEVICES_SELECTION_DISABLED_AMOUNT if settings.DEVICES_SELECTION_DISABLED_AMOUNT > 0 else settings.DEFAULT_DEVICE_LIMIT
        await state.update_data(devices=devices)

        # Переходим к выбору серверов или подтверждению
        await _handle_gift_continue_to_servers_or_confirm(callback, db_user, state, db)
    else:
        # Переходим к выбору устройств
        await callback.message.edit_text(
            text=texts.GIFT_SELECT_DEVICES,
            reply_markup=get_gift_devices_keyboard()
        )

        await state.set_state(GiftSubscriptionStates.selecting_devices)

    await callback.answer()


@error_handler
async def handle_gift_devices_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обработка выбора количества устройств и финальное подтверждение.
    """
    texts = get_texts(db_user.language)

    # Извлекаем количество устройств из callback_data (формат: gift_devices:3)
    _, devices_str = callback.data.split(":")
    devices = int(devices_str)

    # Сохраняем количество устройств в state
    await state.update_data(devices=devices)

    # Переходим к выбору серверов или подтверждению
    await _handle_gift_continue_to_servers_or_confirm(callback, db_user, state, db)
    await callback.answer()


@error_handler
async def handle_gift_confirm_purchase(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обработка подтверждения покупки gift-подписки.
    """
    texts = get_texts(db_user.language)

    # Получаем данные из state
    data = await state.get_data()
    period_days = data.get("period_days")
    traffic_gb = data.get("traffic_gb")
    devices = data.get("devices")
    squads = data.get("squads")

    # Обновляем информацию о пользователе
    db_user = await get_user_by_id(db, db_user.id)

    try:
        # Создаём gift-подписку
        result = await gift_subscription_service.create_gift_subscription(
            db=db,
            user=db_user,
            period_days=period_days,
            traffic_gb=traffic_gb,
            devices=devices,
            squads=squads
        )

        # Формируем текст успеха
        code = result["code"]
        deep_link = result["deep_link"]

        success_text = texts.GIFT_PURCHASE_SUCCESS.format(
            code=code,
            link=deep_link
        )

        # Отправляем сообщение с результатом
        bot_username = settings.BOT_USERNAME.replace("@", "") if settings.BOT_USERNAME else "bot"
        await callback.message.edit_text(
            text=success_text,
            reply_markup=get_gift_share_keyboard(code, bot_username),
            parse_mode="HTML"
        )

        # Очищаем state
        await state.clear()

        await callback.answer("✅ Подарок создан!", show_alert=False)

        logger.info(f"✅ Пользователь {db_user.id} создал gift-подписку: {code}")

    except InsufficientBalanceError as e:
        # Недостаточно средств
        error_text = texts.GIFT_INSUFFICIENT_BALANCE.format(
            required=f"{data.get('price_kopeks', 0)/100:.2f}",
            balance=f"{db_user.balance_kopeks/100:.2f}"
        )

        await callback.message.edit_text(
            text=error_text,
            reply_markup=get_gift_cancel_keyboard()
        )

        await state.clear()
        await callback.answer("❌ Недостаточно средств", show_alert=True)

    except Exception as e:
        logger.error(f"Ошибка создания gift-подписки для user_id={db_user.id}: {e}")

        await callback.message.edit_text(
            text="❌ Произошла ошибка при создании подарка. Попробуйте позже.",
            reply_markup=get_gift_cancel_keyboard()
        )

        await state.clear()
        await callback.answer("❌ Ошибка", show_alert=True)


@error_handler
async def handle_gift_copy_code(callback: types.CallbackQuery):
    """
    Обработка копирования кода (просто показываем уведомление).
    """
    # Код уже в сообщении в <code> теге, пользователь может скопировать его
    await callback.answer("📋 Нажмите на код в сообщении для копирования", show_alert=False)


@error_handler
async def handle_gift_cancel(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    """
    Обработка отмены создания gift-подписки.
    """
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        text=texts.GIFT_CANCEL_MESSAGE,
        reply_markup=get_gift_cancel_keyboard()
    )

    await state.clear()
    await callback.answer()


@error_handler
async def handle_gift_main_menu(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Возврат в главное меню из gift-подписок.
    """
    from app.handlers.menu import show_main_menu

    await state.clear()
    await show_main_menu(callback, db_user, db)
    await callback.answer()


@error_handler
async def handle_gift_back_period(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    """
    Возврат к выбору периода.
    """
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        text=texts.GIFT_SELECT_PERIOD,
        reply_markup=get_gift_period_keyboard()
    )

    await state.set_state(GiftSubscriptionStates.selecting_period)
    await callback.answer()


@error_handler
async def handle_gift_back_traffic(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    """
    Возврат к выбору трафика.
    """
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        text=texts.GIFT_SELECT_TRAFFIC,
        reply_markup=get_gift_traffic_keyboard()
    )

    await state.set_state(GiftSubscriptionStates.selecting_traffic)
    await callback.answer()


@error_handler
async def handle_gift_back_devices(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext
):
    """
    Возврат к выбору устройств.
    """
    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        text=texts.GIFT_SELECT_DEVICES,
        reply_markup=get_gift_devices_keyboard()
    )

    await state.set_state(GiftSubscriptionStates.selecting_devices)
    await callback.answer()


async def show_gift_tariffs_list(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Показывает список тарифов для создания gift-подписки.
    """
    from app.database.crud.tariff import get_tariffs_for_user
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(db_user.language)

    # Получаем доступные тарифы
    promo_group_id = getattr(db_user, 'promo_group_id', None)
    tariffs = await get_tariffs_for_user(db, promo_group_id)

    if not tariffs:
        await callback.message.edit_text(
            "😔 <b>Нет доступных тарифов</b>\n\n"
            "К сожалению, сейчас нет тарифов для создания gift-подписки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Формируем список тарифов
    text = "🎁 <b>Выберите тариф для подарка</b>\n\n"
    text += "Выберите готовый тариф, который получит ваш друг:\n\n"

    buttons = []
    for tariff in tariffs:
        # Формируем описание тарифа
        traffic_text = f"{tariff.traffic_limit_gb} ГБ" if tariff.traffic_limit_gb > 0 else "♾ Безлимит"
        tariff_desc = f"📦 {tariff.name}\n"
        tariff_desc += f"   📊 Трафик: {traffic_text}\n"
        tariff_desc += f"   📱 Устройства: {tariff.device_limit}\n"

        # Минимальная цена из доступных периодов
        if tariff.period_prices:
            min_price = min(tariff.period_prices.values()) / 100
            tariff_desc += f"   💰 От {min_price:.0f}₽"

        text += tariff_desc + "\n"

        # Кнопка выбора тарифа
        buttons.append([
            InlineKeyboardButton(
                text=f"📦 {tariff.name}",
                callback_data=f"gift_tariff:{tariff.id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    await state.set_state(GiftSubscriptionStates.selecting_tariff)
    await callback.answer()


@error_handler
async def handle_gift_tariff_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обработка выбора тарифа для gift-подписки.
    """
    from app.database.crud.tariff import get_tariff_by_id
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(db_user.language)

    # Извлекаем ID тарифа
    _, tariff_id_str = callback.data.split(":")
    tariff_id = int(tariff_id_str)

    # Получаем тариф
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return

    # Сохраняем параметры тарифа в state
    await state.update_data(
        tariff_id=tariff.id,
        traffic_gb=tariff.traffic_limit_gb,
        devices=tariff.device_limit,
        squads=tariff.allowed_squads or []
    )

    # Если у тарифа несколько периодов - показываем выбор
    if tariff.period_prices and len(tariff.period_prices) > 1:
        text = f"🎁 <b>Тариф: {tariff.name}</b>\n\n"
        text += f"📊 Трафик: {tariff.traffic_limit_gb if tariff.traffic_limit_gb > 0 else '♾ Безлимит'} ГБ\n"
        text += f"📱 Устройства: {tariff.device_limit}\n\n"
        text += "Выберите период подписки:\n"

        buttons = []
        for days in sorted(tariff.period_prices.keys()):
            price = tariff.period_prices[days] / 100
            buttons.append([
                InlineKeyboardButton(
                    text=f"{days} дней - {price:.0f}₽",
                    callback_data=f"gift_tariff_period:{days}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(text="🔙 Назад", callback_data="buy_gift_subscription"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        await state.set_state(GiftSubscriptionStates.selecting_period)
    else:
        # Только один период - используем его автоматически
        period_days = list(tariff.period_prices.keys())[0]
        await state.update_data(period_days=period_days)

        # Переходим сразу к подтверждению
        await _show_gift_tariff_confirmation(callback, db_user, state, db, tariff)

    await callback.answer()


@error_handler
async def handle_gift_tariff_period_selection(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обработка выбора периода для тарифа.
    """
    from app.database.crud.tariff import get_tariff_by_id

    # Извлекаем период
    _, period_str = callback.data.split(":")
    period_days = int(period_str)

    # Сохраняем период
    await state.update_data(period_days=period_days)

    # Получаем тариф из state
    data = await state.get_data()
    tariff_id = data.get('tariff_id')

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return

    # Показываем подтверждение
    await _show_gift_tariff_confirmation(callback, db_user, state, db, tariff)
    await callback.answer()


async def _show_gift_tariff_confirmation(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession,
    tariff
):
    """
    Показывает подтверждение покупки gift-подписки по тарифу.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    texts = get_texts(db_user.language)
    data = await state.get_data()

    period_days = data.get("period_days")
    traffic_gb = data.get("traffic_gb")
    devices = data.get("devices")
    squads = data.get("squads", [])

    # Рассчитываем цену
    try:
        price_kopeks = await gift_subscription_service.calculate_gift_price(
            db=db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            devices=devices,
            squads=squads,
            user=db_user
        )
    except Exception as e:
        logger.error(f"Ошибка расчета цены gift-подписки по тарифу: {e}")
        await callback.answer("❌ Ошибка расчета цены", show_alert=True)
        return

    # Сохраняем цену
    await state.update_data(price_kopeks=price_kopeks)

    # Формируем текст подтверждения
    traffic_text = f"{traffic_gb} ГБ" if traffic_gb > 0 else "♾ Безлимит"
    period_text = f"{period_days} дней"

    confirm_text = f"🎁 <b>Подарочная подписка</b>\n\n"
    confirm_text += f"📦 Тариф: {tariff.name}\n"
    confirm_text += f"📅 Период: {period_text}\n"
    confirm_text += f"📊 Трафик: {traffic_text}\n"
    confirm_text += f"📱 Устройства: {devices}\n\n"
    confirm_text += f"💰 <b>Стоимость: {price_kopeks/100:.2f} ₽</b>\n\n"
    confirm_text += "Подтвердите покупку:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Купить подарок", callback_data="gift_confirm_purchase")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="buy_gift_subscription")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel")]
    ])

    await callback.message.edit_text(
        text=confirm_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    await state.set_state(GiftSubscriptionStates.confirming_purchase)


async def _handle_gift_continue_to_servers_or_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Вспомогательная функция: определяет нужно ли показывать выбор серверов
    или сразу перейти к подтверждению.
    """
    from app.handlers.subscription.countries import _get_available_countries

    texts = get_texts(db_user.language)

    # Получаем доступные серверы с учетом promo_group
    promo_group_id = db_user.promo_group_id if hasattr(db_user, 'promo_group_id') else None
    available_countries = await _get_available_countries(promo_group_id)

    # Фильтруем только доступные серверы (is_available=True)
    available_countries = [c for c in available_countries if c.get('is_available', True)]

    if len(available_countries) == 0:
        # Нет доступных серверов - ошибка
        await callback.answer("❌ Нет доступных серверов", show_alert=True)
        return
    elif len(available_countries) == 1:
        # Только один сервер - используем автоматически
        squad_uuid = available_countries[0]['uuid']
        await state.update_data(squads=[squad_uuid])

        logger.info(f"Gift: Только 1 сервер доступен ({squad_uuid}), автоматический выбор")

        # Переходим к подтверждению
        await _show_gift_confirmation(callback, db_user, state, db)
    else:
        # Несколько серверов - показываем выбор
        logger.info(f"Gift: Доступно {len(available_countries)} серверов, показываем выбор")

        # Автоматически предвыбираем бесплатные серверы
        free_servers = [c['uuid'] for c in available_countries if c.get('price_kopeks', 0) == 0]
        if not free_servers:
            # Если нет бесплатных, предвыбираем первый
            free_servers = [available_countries[0]['uuid']]

        await state.update_data(squads=free_servers, available_countries=available_countries)

        # Импортируем клавиатуру для выбора стран
        from app.keyboards.subscription import get_countries_keyboard

        await callback.message.edit_text(
            text=texts.GIFT_SELECT_COUNTRIES,
            reply_markup=get_countries_keyboard(available_countries, free_servers, db_user.language),
            parse_mode="HTML"
        )

        await state.set_state(GiftSubscriptionStates.selecting_countries)


async def _show_gift_confirmation(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Показывает подтверждение покупки gift-подписки.
    """
    texts = get_texts(db_user.language)
    data = await state.get_data()

    period_days = data.get("period_days")
    traffic_gb = data.get("traffic_gb")
    devices = data.get("devices")
    squads = data.get("squads", [])

    # Рассчитываем цену
    try:
        price_kopeks = await gift_subscription_service.calculate_gift_price(
            db=db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            devices=devices,
            squads=squads,
            user=db_user
        )
    except Exception as e:
        logger.error(f"Ошибка расчета цены gift-подписки: {e}")
        await callback.answer("❌ Ошибка расчета цены", show_alert=True)
        return

    # Сохраняем цену в state
    await state.update_data(price_kopeks=price_kopeks)

    # Формируем текст подтверждения
    traffic_text = f"{traffic_gb} ГБ" if traffic_gb > 0 else "♾ Безлимит"
    period_text = texts.get(f"GIFT_PERIOD_{period_days}_DAYS", f"{period_days} дней")

    # Получаем названия выбранных серверов
    available_countries = data.get("available_countries", [])
    if available_countries:
        selected_names = [c['name'] for c in available_countries if c['uuid'] in squads]
        countries_text = ", ".join(selected_names) if selected_names else "Авто"
    else:
        countries_text = "Авто"

    confirm_text = texts.GIFT_CONFIRM_PURCHASE.format(
        period=period_text,
        traffic=traffic_text,
        devices=devices,
        countries=countries_text,
        price=f"{price_kopeks/100:.2f}"
    )

    # Показываем подтверждение
    await callback.message.edit_text(
        text=confirm_text,
        reply_markup=get_gift_confirm_keyboard(price_kopeks / 100)
    )

    await state.set_state(GiftSubscriptionStates.confirming_purchase)


@error_handler
async def handle_gift_select_country(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Обработка выбора/отмены выбора сервера для gift-подписки.
    """
    # Извлекаем UUID сервера из callback_data (формат: country_UUID или gift_country_UUID)
    parts = callback.data.split('_')
    country_uuid = '_'.join(parts[1:])  # Все после первого underscore

    data = await state.get_data()
    selected_countries = data.get('squads', [])

    # Переключаем выбор сервера
    if country_uuid in selected_countries:
        selected_countries.remove(country_uuid)
    else:
        selected_countries.append(country_uuid)

    # Обновляем state
    data['squads'] = selected_countries
    await state.set_data(data)

    # Получаем список доступных серверов для обновления клавиатуры
    available_countries = data.get('available_countries', [])

    from app.keyboards.subscription import get_countries_keyboard

    # Обновляем клавиатуру
    await callback.message.edit_reply_markup(
        reply_markup=get_countries_keyboard(available_countries, selected_countries, db_user.language)
    )

    await callback.answer()


@error_handler
async def handle_gift_countries_continue(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    db: AsyncSession
):
    """
    Подтверждение выбора серверов и переход к финальному подтверждению.
    """
    data = await state.get_data()
    selected_squads = data.get('squads', [])

    if not selected_squads:
        await callback.answer("❌ Выберите хотя бы один сервер", show_alert=True)
        return

    # Переходим к подтверждению покупки
    await _show_gift_confirmation(callback, db_user, state, db)
    await callback.answer()


def register_gift_subscription_handlers(dp: Dispatcher):
    """
    Регистрирует все handlers для gift-подписок.
    """
    # Начало flow - кнопка "Подарить подписку"
    dp.callback_query.register(
        start_gift_subscription_flow,
        F.data == "buy_gift_subscription"
    )

    # Режим тарифов - выбор тарифа
    dp.callback_query.register(
        handle_gift_tariff_selection,
        F.data.startswith("gift_tariff:"),
        GiftSubscriptionStates.selecting_tariff
    )

    # Режим тарифов - выбор периода для тарифа
    dp.callback_query.register(
        handle_gift_tariff_period_selection,
        F.data.startswith("gift_tariff_period:"),
        GiftSubscriptionStates.selecting_period
    )

    # Классический режим - выбор периода
    dp.callback_query.register(
        handle_gift_period_selection,
        F.data.startswith("gift_period:"),
        GiftSubscriptionStates.selecting_period
    )

    # Выбор трафика
    dp.callback_query.register(
        handle_gift_traffic_selection,
        F.data.startswith("gift_traffic:"),
        GiftSubscriptionStates.selecting_traffic
    )

    # Выбор устройств
    dp.callback_query.register(
        handle_gift_devices_selection,
        F.data.startswith("gift_devices:"),
        GiftSubscriptionStates.selecting_devices
    )

    # Выбор серверов
    dp.callback_query.register(
        handle_gift_select_country,
        F.data.startswith("country_"),
        GiftSubscriptionStates.selecting_countries
    )

    # Подтверждение выбора серверов и переход дальше
    dp.callback_query.register(
        handle_gift_countries_continue,
        F.data == "countries_continue",
        GiftSubscriptionStates.selecting_countries
    )

    # Подтверждение покупки
    dp.callback_query.register(
        handle_gift_confirm_purchase,
        F.data == "gift_confirm_purchase",
        GiftSubscriptionStates.confirming_purchase
    )

    # Копирование кода
    dp.callback_query.register(
        handle_gift_copy_code,
        F.data.startswith("gift_copy_code:")
    )

    # Отмена
    dp.callback_query.register(
        handle_gift_cancel,
        F.data == "gift_cancel"
    )

    # Главное меню (из gift-подписок)
    dp.callback_query.register(
        handle_gift_main_menu,
        F.data == "main_menu"
    )

    # Кнопки "Назад"
    dp.callback_query.register(
        handle_gift_back_period,
        F.data == "gift_back_period"
    )

    dp.callback_query.register(
        handle_gift_back_traffic,
        F.data == "gift_back_traffic"
    )

    dp.callback_query.register(
        handle_gift_back_devices,
        F.data == "gift_back_devices"
    )

    logger.info("✅ Gift subscription handlers registered")
