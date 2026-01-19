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
    state: FSMContext
):
    """
    Точка входа в flow создания подарочной подписки.
    """
    texts = get_texts(db_user.language)

    # Очищаем state и начинаем новый flow
    await state.clear()

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

        # Получаем случайный доступный сервер (как для trial)
        from app.database.crud.server_squad import get_random_trial_squad_uuid
        try:
            squad_uuid = await get_random_trial_squad_uuid(db)
            if not squad_uuid:
                await callback.answer("❌ Нет доступных серверов", show_alert=True)
                return
            squads = [squad_uuid]
        except Exception as e:
            logger.error(f"Ошибка получения сервера для gift: {e}")
            await callback.answer("❌ Ошибка получения сервера", show_alert=True)
            return

        await state.update_data(squads=squads)

        # Получаем данные из state для расчёта цены
        data = await state.get_data()
        period_days = data.get("period_days")

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

        confirm_text = texts.GIFT_CONFIRM_PURCHASE.format(
            period=period_text,
            traffic=traffic_text,
            devices=devices,
            countries="Авто",  # Автоматический выбор сервера
            price=f"{price_kopeks/100:.2f}"
        )

        # Показываем подтверждение
        await callback.message.edit_text(
            text=confirm_text,
            reply_markup=get_gift_confirm_keyboard(price_kopeks / 100)
        )

        await state.set_state(GiftSubscriptionStates.confirming_purchase)
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

    # Получаем случайный доступный сервер (как для trial)
    from app.database.crud.server_squad import get_random_trial_squad_uuid
    try:
        squad_uuid = await get_random_trial_squad_uuid(db)
        if not squad_uuid:
            await callback.answer("❌ Нет доступных серверов", show_alert=True)
            return
        squads = [squad_uuid]
    except Exception as e:
        logger.error(f"Ошибка получения сервера для gift: {e}")
        await callback.answer("❌ Ошибка получения сервера", show_alert=True)
        return

    await state.update_data(squads=squads)

    # Получаем данные из state для расчёта цены
    data = await state.get_data()
    period_days = data.get("period_days")
    traffic_gb = data.get("traffic_gb")

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

    confirm_text = texts.GIFT_CONFIRM_PURCHASE.format(
        period=period_text,
        traffic=traffic_text,
        devices=devices,
        countries="Авто",  # Автоматический выбор сервера
        price=f"{price_kopeks/100:.2f}"
    )

    # Показываем подтверждение
    await callback.message.edit_text(
        text=confirm_text,
        reply_markup=get_gift_confirm_keyboard(price_kopeks / 100)
    )

    await state.set_state(GiftSubscriptionStates.confirming_purchase)
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


def register_gift_subscription_handlers(dp: Dispatcher):
    """
    Регистрирует все handlers для gift-подписок.
    """
    # Начало flow - кнопка "Подарить подписку"
    dp.callback_query.register(
        start_gift_subscription_flow,
        F.data == "buy_gift_subscription"
    )

    # Выбор периода
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
