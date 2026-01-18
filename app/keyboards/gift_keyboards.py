"""
Клавиатуры для работы с подарочными подписками.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.config import PERIOD_PRICES, get_traffic_prices


def get_gift_period_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора периода gift-подписки из PERIOD_PRICES.

    Returns:
        InlineKeyboardMarkup с доступными периодами
    """
    buttons = []

    # Используем периоды из конфига
    for days in sorted(PERIOD_PRICES.keys()):
        price = PERIOD_PRICES[days] / 100  # в рублях
        button_text = f"{days} дней ({price:.0f}₽)"
        buttons.append([
            InlineKeyboardButton(text=button_text, callback_data=f"gift_period:{days}")
        ])

    buttons.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_gift_traffic_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора трафика gift-подписки из TRAFFIC_PRICES.

    Returns:
        InlineKeyboardMarkup с вариантами трафика
    """
    buttons = []

    # Используем пакеты трафика из конфига
    traffic_prices = get_traffic_prices()
    for gb in sorted(traffic_prices.keys()):
        if gb == 0:
            button_text = "♾ Безлимит"
        else:
            price = traffic_prices[gb] / 100  # в рублях
            button_text = f"{gb} ГБ (+{price:.0f}₽)"
        buttons.append([
            InlineKeyboardButton(text=button_text, callback_data=f"gift_traffic:{gb}")
        ])

    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="gift_back_period"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_gift_devices_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора количества устройств gift-подписки.

    Returns:
        InlineKeyboardMarkup с вариантами: 1/3/5 устройств
    """
    # Используем стандартные варианты устройств
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 устройство", callback_data="gift_devices:1"),
        ],
        [
            InlineKeyboardButton(text="3 устройства", callback_data="gift_devices:3"),
        ],
        [
            InlineKeyboardButton(text="5 устройств", callback_data="gift_devices:5"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="gift_back_traffic"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
        ]
    ])
    return keyboard


def get_gift_confirm_keyboard(price_rubles: float) -> InlineKeyboardMarkup:
    """
    Клавиатура подтверждения покупки gift-подписки.

    Args:
        price_rubles: Итоговая цена в рублях

    Returns:
        InlineKeyboardMarkup с кнопками подтверждения/отмены
    """
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ Купить за {price_rubles:.2f}₽",
                callback_data="gift_confirm_purchase"
            ),
        ],
        [
            InlineKeyboardButton(text="⬅️ Изменить", callback_data="gift_back_devices"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
        ]
    ])
    return keyboard


def get_gift_share_keyboard(code: str, bot_username: str) -> InlineKeyboardMarkup:
    """
    Клавиатура после успешной покупки gift-подписки.

    Args:
        code: Код активации gift-подписки
        bot_username: Username бота (без @)

    Returns:
        InlineKeyboardMarkup с кнопками для отправки подарка
    """
    # Формируем deep link
    deep_link = f"https://t.me/{bot_username}?start={code}"

    # URL для кнопки "Поделиться" через Telegram
    share_text = f"🎁 Я подарил тебе VPN-подписку! Активируй её здесь: {deep_link}"
    share_url = f"https://t.me/share/url?url={deep_link}&text={share_text}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📤 Отправить другу",
                url=share_url
            ),
        ],
        [
            InlineKeyboardButton(
                text="📋 Скопировать код",
                callback_data=f"gift_copy_code:{code}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🏠 Главное меню",
                callback_data="main_menu"
            ),
        ]
    ])
    return keyboard


def get_gift_cancel_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура при отмене создания gift-подписки.

    Returns:
        InlineKeyboardMarkup с кнопкой возврата в главное меню
    """
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🏠 Главное меню",
                callback_data="main_menu"
            ),
        ]
    ])
    return keyboard
