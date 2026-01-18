"""
Клавиатуры для работы с подарочными подписками.
"""
from typing import List
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_gift_period_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора периода gift-подписки.

    Returns:
        InlineKeyboardMarkup с периодами: 7/30/90/180 дней
    """
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="7 дней", callback_data="gift_period:7"),
            InlineKeyboardButton(text="30 дней", callback_data="gift_period:30"),
        ],
        [
            InlineKeyboardButton(text="90 дней", callback_data="gift_period:90"),
            InlineKeyboardButton(text="180 дней", callback_data="gift_period:180"),
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
        ]
    ])
    return keyboard


def get_gift_traffic_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора трафика gift-подписки.

    Returns:
        InlineKeyboardMarkup с вариантами трафика: 50/100/200/безлимит ГБ
    """
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="50 ГБ", callback_data="gift_traffic:50"),
            InlineKeyboardButton(text="100 ГБ", callback_data="gift_traffic:100"),
        ],
        [
            InlineKeyboardButton(text="200 ГБ", callback_data="gift_traffic:200"),
            InlineKeyboardButton(text="♾ Безлимит", callback_data="gift_traffic:0"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="gift_back_period"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
        ]
    ])
    return keyboard


def get_gift_devices_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора количества устройств gift-подписки.

    Returns:
        InlineKeyboardMarkup с вариантами: 1/3/5 устройств
    """
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


def get_gift_countries_keyboard(squads: List[dict]) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора серверов (стран) gift-подписки.

    Args:
        squads: Список доступных серверов/сквадов из БД
                Формат: [{"uuid": "...", "name": "...", "flag": "🇷🇺"}, ...]

    Returns:
        InlineKeyboardMarkup с доступными странами
    """
    buttons = []

    # Создаем кнопки для каждого сквада
    for squad in squads:
        flag = squad.get("flag", "🌍")
        name = squad.get("name", "Unknown")
        uuid = squad.get("uuid", "")

        button_text = f"{flag} {name}"
        buttons.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"gift_country:{uuid}"
            )
        ])

    # Добавляем кнопки управления
    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="gift_back_devices"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
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
            InlineKeyboardButton(text="⬅️ Изменить", callback_data="gift_back_countries"),
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
