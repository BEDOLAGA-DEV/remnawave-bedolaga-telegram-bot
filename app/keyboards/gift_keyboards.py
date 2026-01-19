"""
Клавиатуры для работы с подарочными подписками.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.config import settings, PERIOD_PRICES


def get_gift_period_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора периода gift-подписки из доступных периодов.

    Returns:
        InlineKeyboardMarkup с доступными периодами
    """
    buttons = []

    # Используем только включенные периоды из конфига
    available_periods = settings.get_available_subscription_periods()

    for days in sorted(available_periods):
        price = PERIOD_PRICES.get(days, 0) / 100  # в рублях
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
    Клавиатура выбора трафика gift-подписки из доступных пакетов.

    Returns:
        InlineKeyboardMarkup с вариантами трафика
    """
    buttons = []

    # Используем только включенные пакеты трафика из конфига
    all_packages = settings.get_traffic_packages()
    enabled_packages = [pkg for pkg in all_packages if pkg['enabled']]

    # Сортируем по GB
    enabled_packages.sort(key=lambda x: x['gb'])

    for pkg in enabled_packages:
        gb = pkg['gb']
        price = pkg['price']

        if gb == 0:
            button_text = "♾ Безлимит"
        else:
            button_text = f"{gb} ГБ (+{price / 100:.0f}₽)"

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
        InlineKeyboardMarkup с вариантами устройств из конфига
    """
    buttons = []

    # Используем настройки из конфига
    start_devices = settings.DEFAULT_DEVICE_LIMIT
    max_devices = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else 50
    end_devices = min(max_devices + 1, start_devices + 10)

    for devices in range(start_devices, end_devices):
        # Расчет доплаты за дополнительные устройства
        price = max(0, devices - settings.DEFAULT_DEVICE_LIMIT) * settings.PRICE_PER_DEVICE
        price_text = f" (+{price / 100:.0f}₽)" if price > 0 else " (вкл.)"

        # Форматирование текста кнопки
        device_word = "устройство" if devices == 1 else ("устройства" if 2 <= devices <= 4 else "устройств")
        button_text = f"{devices} {device_word}{price_text}"

        buttons.append([
            InlineKeyboardButton(text=button_text, callback_data=f"gift_devices:{devices}")
        ])

    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="gift_back_traffic"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="gift_cancel"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


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
