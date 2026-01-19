from typing import List, Optional
from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timezone
from app.database.models import User
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings, PERIOD_PRICES, TRAFFIC_PRICES
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.pricing_utils import (
    format_period_description,
    apply_percentage_discount,
)
from app.utils.price_display import PriceInfo, format_price_button
from app.utils.subscription_utils import (
    get_display_subscription_link,
    get_happ_cryptolink_redirect_link,
)
import logging

logger = logging.getLogger(__name__)


async def get_main_menu_keyboard_async(
    db: AsyncSession,
    language: str = DEFAULT_LANGUAGE,
    is_admin: bool = False,
    has_had_paid_subscription: bool = False,
    has_active_subscription: bool = False,
    subscription_is_active: bool = False,
    balance_kopeks: int = 0,
    subscription=None,
    show_resume_checkout: bool = False,
    has_saved_cart: bool = False,
    *,
    is_moderator: bool = False,
    custom_buttons: Optional[list[InlineKeyboardButton]] = None,
    user=None,  # Добавляем параметр пользователя для получения данных
) -> InlineKeyboardMarkup:
    """
    Асинхронная версия get_main_menu_keyboard с поддержкой конструктора меню.

    Если MENU_LAYOUT_ENABLED=True, использует конфигурацию из БД.
    Иначе делегирует в синхронную версию.
    """
    if settings.MENU_LAYOUT_ENABLED:
        from app.services.menu_layout_service import MenuLayoutService, MenuContext
        from datetime import datetime

        # Получаем данные для плейсхолдеров
        subscription_days_left = 0
        traffic_used_gb = 0.0
        traffic_left_gb = 0.0
        referral_count = 0
        referral_earnings_kopeks = 0
        registration_days = 0
        promo_group_id = None
        has_autopay = False
        username = ""

        # Заполняем данными из подписки
        if subscription:
            # Дни до окончания подписки
            if hasattr(subscription, 'days_left'):
                # Используем свойство из модели, которое правильно вычисляет дни в UTC
                subscription_days_left = subscription.days_left
            elif hasattr(subscription, 'end_date') and subscription.end_date:
                # Fallback: вычисляем вручную, используя UTC
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                days_left = (subscription.end_date - now_utc).days
                subscription_days_left = max(0, days_left)
            
            # Трафик
            if hasattr(subscription, 'traffic_used_gb'):
                traffic_used_gb = subscription.traffic_used_gb or 0.0
            
            if hasattr(subscription, 'traffic_limit_gb') and subscription.traffic_limit_gb:
                traffic_left_gb = max(0, subscription.traffic_limit_gb - (subscription.traffic_used_gb or 0))
            
            # Автоплатеж
            if hasattr(subscription, 'autopay_enabled'):
                has_autopay = subscription.autopay_enabled

        # Получаем данные пользователя
        if user:
            # Имя пользователя
            if hasattr(user, 'username') and user.username:
                username = user.username
            elif hasattr(user, 'first_name') and user.first_name:
                username = user.first_name
            
            # Дни с регистрации
            if hasattr(user, 'created_at') and user.created_at:
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                registration_days = (now_utc - user.created_at).days
            
            # ID промо-группы
            if hasattr(user, 'promo_group_id'):
                promo_group_id = user.promo_group_id

        # Получаем данные о рефералах из БД (если нужно)
        try:
            from app.database.crud.referral import get_user_referral_stats
            if user and hasattr(user, 'id'):
                referral_data = await get_user_referral_stats(db, user.id)
                if referral_data:
                    referral_count = referral_data.get('invited_count', 0)
                    referral_earnings_kopeks = referral_data.get('total_earned_kopeks', 0)
        except Exception as e:
            logger.error(f"Error getting referral data: {e}")

        context = MenuContext(
            language=language,
            is_admin=is_admin,
            is_moderator=is_moderator,
            has_active_subscription=has_active_subscription,
            subscription_is_active=subscription_is_active,
            has_had_paid_subscription=has_had_paid_subscription,
            balance_kopeks=balance_kopeks,
            subscription=subscription,
            show_resume_checkout=show_resume_checkout,
            has_saved_cart=has_saved_cart,
            custom_buttons=custom_buttons or [],
            # Добавляем данные для плейсхолдеров
            username=username,
            subscription_days=subscription_days_left,
            traffic_used_gb=traffic_used_gb,
            traffic_left_gb=traffic_left_gb,
            referral_count=referral_count,
            referral_earnings_kopeks=referral_earnings_kopeks,
            registration_days=registration_days,
            promo_group_id=promo_group_id,
            has_autopay=has_autopay,
        )

        return await MenuLayoutService.build_keyboard(db, context)

    # Fallback на синхронную версию
    return get_main_menu_keyboard(
        language=language,
        is_admin=is_admin,
        has_had_paid_subscription=has_had_paid_subscription,
        has_active_subscription=has_active_subscription,
        subscription_is_active=subscription_is_active,
        balance_kopeks=balance_kopeks,
        subscription=subscription,
        show_resume_checkout=show_resume_checkout,
        has_saved_cart=has_saved_cart,
        is_moderator=is_moderator,
        custom_buttons=custom_buttons,
    )


def _get_localized_value(values, language: str, default_language: str = "en") -> str:
    if not isinstance(values, dict):
        return ""

    candidates = []
    normalized_language = (language or "").strip().lower()

    if normalized_language:
        candidates.append(normalized_language)
        if "-" in normalized_language:
            candidates.append(normalized_language.split("-")[0])

    default_language = (default_language or "").strip().lower()
    if default_language and default_language not in candidates:
        candidates.append(default_language)

    for candidate in candidates:
        if not candidate:
            continue
        value = values.get(candidate)
        if isinstance(value, str) and value.strip():
            return value

    for value in values.values():
        if isinstance(value, str) and value.strip():
            return value

    return ""


def _build_additional_buttons(additional_section, language: str) -> List[InlineKeyboardButton]:
    if not isinstance(additional_section, dict):
        return []

    buttons = additional_section.get("buttons")
    if not isinstance(buttons, list):
        return []

    localized_buttons: List[InlineKeyboardButton] = []

    for button in buttons:
        if not isinstance(button, dict):
            continue

        button_text = _get_localized_value(button.get("buttonText"), language)
        button_link = button.get("buttonLink")

        if not button_text or not button_link:
            continue

        localized_buttons.append(
            InlineKeyboardButton(text=button_text, url=button_link)
        )

    return localized_buttons


_LANGUAGE_DISPLAY_NAMES = {
    "ru": "🇷🇺 Русский",
    "ru-ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "en-us": "🇺🇸 English",
    "en-gb": "🇬🇧 English",
    "ua": "🇺🇦 Українська",
    "uk": "🇺🇦 Українська",
    "uk-ua": "🇺🇦 Українська",
    "kk": "🇰🇿 Қазақша",
    "kk-kz": "🇰🇿 Қазақша",
    "kz": "🇰🇿 Қазақша",
    "uz": "🇺🇿 Oʻzbekcha",
    "uz-uz": "🇺🇿 Oʻzbekcha",
    "tr": "🇹🇷 Türkçe",
    "tr-tr": "🇹🇷 Türkçe",
    "pl": "🇵🇱 Polski",
    "pl-pl": "🇵🇱 Polski",
    "de": "🇩🇪 Deutsch",
    "de-de": "🇩🇪 Deutsch",
    "fr": "🇫🇷 Français",
    "fr-fr": "🇫🇷 Français",
    "es": "🇪🇸 Español",
    "es-es": "🇪🇸 Español",
    "it": "🇮🇹 Italiano",
    "it-it": "🇮🇹 Italiano",
    "pt": "🇵🇹 Português",
    "pt-pt": "🇵🇹 Português",
    "pt-br": "🇧🇷 Português",
    "zh": "🇨🇳 中文",
    "zh-cn": "🇨🇳 中文 (简体)",
    "zh-hans": "🇨🇳 中文 (简体)",
    "zh-tw": "🇹🇼 中文 (繁體)",
    "zh-hant": "🇹🇼 中文 (繁體)",
    "vi": "🇻🇳 Tiếng Việt",
    "vi-vn": "🇻🇳 Tiếng Việt",
}

def get_rules_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=texts.RULES_ACCEPT, callback_data="rules_accept"),
            InlineKeyboardButton(text=texts.RULES_DECLINE, callback_data="rules_decline")
        ]
    ])

def get_privacy_policy_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.PRIVACY_POLICY_ACCEPT,
                callback_data="privacy_policy_accept"
            ),
            InlineKeyboardButton(
                text=texts.PRIVACY_POLICY_DECLINE,
                callback_data="privacy_policy_decline"
            )
        ]
    ])

def get_channel_sub_keyboard(
    channel_link: Optional[str],
    language: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    buttons: List[List[InlineKeyboardButton]] = []

    if channel_link:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t("CHANNEL_SUBSCRIBE_BUTTON", "🔗 Подписаться"),
                    url=channel_link,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t("CHANNEL_CHECK_BUTTON", "✅ Я подписался"),
                callback_data="sub_channel_check",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_post_registration_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("POST_REGISTRATION_TRIAL_BUTTON", "🚀 Подключиться бесплатно 🚀"),
                callback_data="trial_activate"
            )
        ],
        [InlineKeyboardButton(text=texts.t("SKIP_BUTTON", "Пропустить ➡️"), callback_data="back_to_menu")],
    ])


def get_language_selection_keyboard(
    current_language: Optional[str] = None,
    *,
    include_back: bool = False,
    language: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    available_languages = settings.get_available_languages()

    buttons: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []

    normalized_current = (current_language or "").lower()

    for index, lang_code in enumerate(available_languages, start=1):
        normalized_code = lang_code.lower()
        display_name = _LANGUAGE_DISPLAY_NAMES.get(
            normalized_code,
            normalized_code.upper(),
        )

        prefix = "✅ " if normalized_code == normalized_current and normalized_current else ""

        row.append(
            InlineKeyboardButton(
                text=f"{prefix}{display_name}",
                callback_data=f"language_select:{normalized_code}",
            )
        )

        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    if include_back:
        texts = get_texts(language)
        buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_text_main_menu_keyboard(
    language: str,
    texts,
    *,
    is_admin: bool,
    is_moderator: bool,
) -> InlineKeyboardMarkup:
    profile_text = texts.t("MENU_PROFILE", "👤 Личный кабинет")
    miniapp_url = settings.get_main_menu_miniapp_url()

    if miniapp_url:
        profile_button = InlineKeyboardButton(
            text=profile_text,
            web_app=types.WebAppInfo(url=miniapp_url),
        )
    else:
        profile_button = InlineKeyboardButton(
            text=profile_text,
            callback_data="menu_profile_unavailable",
        )

    keyboard_rows: List[List[InlineKeyboardButton]] = [[profile_button]]

    if settings.is_language_selection_enabled():
        keyboard_rows.append([
            InlineKeyboardButton(text=texts.MENU_LANGUAGE, callback_data="menu_language")
        ])

    support_enabled = False
    try:
        from app.services.support_settings_service import SupportSettingsService

        support_enabled = SupportSettingsService.is_support_menu_enabled()
    except Exception:
        support_enabled = settings.SUPPORT_MENU_ENABLED

    if support_enabled:
        keyboard_rows.append([
            InlineKeyboardButton(text=texts.MENU_SUPPORT, callback_data="menu_support")
        ])

    if is_admin:
        keyboard_rows.append([
            InlineKeyboardButton(text=texts.MENU_ADMIN, callback_data="admin_panel")
        ])
    elif is_moderator:
        keyboard_rows.append([
            InlineKeyboardButton(text="🧑‍⚖️ Модерация", callback_data="moderator_panel")
        ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def get_main_menu_keyboard(
    language: str = DEFAULT_LANGUAGE,
    is_admin: bool = False,
    has_had_paid_subscription: bool = False,
    has_active_subscription: bool = False,
    subscription_is_active: bool = False,
    balance_kopeks: int = 0,
    subscription=None,
    show_resume_checkout: bool = False,
    has_saved_cart: bool = False,  # Новый параметр для отображения уведомления о сохраненной корзине
    *,
    is_moderator: bool = False,
    custom_buttons: Optional[list[InlineKeyboardButton]] = None,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    if settings.is_text_main_menu_mode():
        return _build_text_main_menu_keyboard(
            language,
            texts,
            is_admin=is_admin,
            is_moderator=is_moderator,
        )
    
    if settings.DEBUG:
        print(f"DEBUG KEYBOARD: language={language}, is_admin={is_admin}, has_had_paid={has_had_paid_subscription}, has_active={has_active_subscription}, sub_active={subscription_is_active}, balance={balance_kopeks}")
    
    safe_balance = balance_kopeks or 0
    if hasattr(texts, 'BALANCE_BUTTON') and safe_balance > 0:
        balance_button_text = texts.BALANCE_BUTTON.format(balance=texts.format_price(safe_balance))
    else:
        balance_button_text = texts.t(
            "BALANCE_BUTTON_DEFAULT",
            "💰 Баланс: {balance}",
        ).format(balance=texts.format_price(safe_balance))
    
    keyboard: list[list[InlineKeyboardButton]] = []
    paired_buttons: list[InlineKeyboardButton] = []

    if has_active_subscription and subscription_is_active:
        connect_mode = settings.CONNECT_BUTTON_MODE
        subscription_link = get_display_subscription_link(subscription)

        def _fallback_connect_button() -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                callback_data="subscription_connect",
            )

        if connect_mode == "miniapp_subscription":
            if subscription_link:
                keyboard.append([
                    InlineKeyboardButton(
                        text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                        web_app=types.WebAppInfo(url=subscription_link)
                    )
                ])
            else:
                keyboard.append([_fallback_connect_button()])
        elif connect_mode == "miniapp_custom":
            keyboard.append([
                InlineKeyboardButton(
                    text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                    web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL)
                )
            ])
        elif connect_mode == "link":
            if subscription_link:
                keyboard.append([
                    InlineKeyboardButton(
                        text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                        url=subscription_link
                    )
                ])
            else:
                keyboard.append([_fallback_connect_button()])
        elif connect_mode == "happ_cryptolink":
            if subscription_link:
                keyboard.append([
                    InlineKeyboardButton(
                        text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                        callback_data="open_subscription_link",
                    )
                ])
            else:
                keyboard.append([_fallback_connect_button()])
        else:
            keyboard.append([_fallback_connect_button()])

        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            keyboard.append(happ_row)
        paired_buttons.append(
            InlineKeyboardButton(text=texts.MENU_SUBSCRIPTION, callback_data="menu_subscription")
        )

        # Добавляем кнопку докупки трафика для лимитированных подписок
        # В режиме тарифов проверяем tariff_id (детальная проверка в хендлере)
        # В классическом режиме проверяем глобальные настройки
        show_traffic_topup = False
        if subscription and not subscription.is_trial and (subscription.traffic_limit_gb or 0) > 0:
            if settings.is_tariffs_mode() and getattr(subscription, 'tariff_id', None):
                # Режим тарифов - показываем кнопку, проверка настроек тарифа в хендлере
                show_traffic_topup = settings.BUY_TRAFFIC_BUTTON_VISIBLE
            elif settings.is_traffic_topup_enabled() and not settings.is_traffic_topup_blocked():
                # Классический режим - проверяем глобальные настройки
                show_traffic_topup = settings.BUY_TRAFFIC_BUTTON_VISIBLE

        if show_traffic_topup:
            paired_buttons.append(
                InlineKeyboardButton(text=texts.t("BUY_TRAFFIC_BUTTON", "📈 Докупить трафик"), callback_data="buy_traffic")
            )

    keyboard.append([InlineKeyboardButton(text=balance_button_text, callback_data="menu_balance")])
    
    show_trial = not has_had_paid_subscription and not has_active_subscription

    show_buy = not has_active_subscription or not subscription_is_active
    current_subscription = subscription
    has_active_paid_subscription = bool(
        current_subscription
        and not getattr(current_subscription, "is_trial", False)
        and getattr(current_subscription, "is_active", False)
    )
    simple_purchase_button = None
    if settings.SIMPLE_SUBSCRIPTION_ENABLED:
        simple_purchase_button = InlineKeyboardButton(
            text=texts.MENU_SIMPLE_SUBSCRIPTION,
            callback_data="simple_subscription_purchase",
        )

    subscription_buttons: list[InlineKeyboardButton] = []

    if show_trial:
        subscription_buttons.append(
            InlineKeyboardButton(text=texts.MENU_TRIAL, callback_data="menu_trial")
        )
    
    if show_buy:
        subscription_buttons.append(
            InlineKeyboardButton(text=texts.MENU_BUY_SUBSCRIPTION, callback_data="menu_buy")
        )
    
    if subscription_buttons:
        paired_buttons.extend(subscription_buttons)
    if simple_purchase_button:
        paired_buttons.append(simple_purchase_button)

    if show_resume_checkout or has_saved_cart:
        resume_callback = (
            "return_to_saved_cart" if has_saved_cart else "subscription_resume_checkout"
        )
        paired_buttons.append(
            InlineKeyboardButton(
                text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                callback_data=resume_callback,
            )
        )

    if custom_buttons:
        for button in custom_buttons:
            if isinstance(button, InlineKeyboardButton):
                paired_buttons.append(button)

    # Добавляем кнопки промокода и рефералов, учитывая настройки
    paired_buttons.append(
        InlineKeyboardButton(text=texts.MENU_PROMOCODE, callback_data="menu_promocode")
    )

    # Добавляем кнопку подарочной подписки, только если функция включена
    if settings.GIFT_SUBSCRIPTIONS_ENABLED:
        paired_buttons.append(
            InlineKeyboardButton(text=texts.GIFT_SUBSCRIPTION_MENU_BUTTON, callback_data="buy_gift_subscription")
        )

    # Добавляем кнопку рефералов, только если программа включена
    if settings.is_referral_program_enabled():
        paired_buttons.append(
            InlineKeyboardButton(text=texts.MENU_REFERRALS, callback_data="menu_referrals")
        )

    # Добавляем кнопку конкурсов
    if settings.CONTESTS_ENABLED and settings.CONTESTS_BUTTON_VISIBLE:
        paired_buttons.append(
            InlineKeyboardButton(text=texts.t("CONTESTS_BUTTON", "🎲 Конкурсы"), callback_data="contests_menu")
        )

    try:
        from app.services.support_settings_service import SupportSettingsService
        support_enabled = SupportSettingsService.is_support_menu_enabled()
    except Exception:
        support_enabled = settings.SUPPORT_MENU_ENABLED

    if support_enabled:
        paired_buttons.append(
            InlineKeyboardButton(text=texts.MENU_SUPPORT, callback_data="menu_support")
        )

    # Добавляем кнопку активации
    if settings.ACTIVATE_BUTTON_VISIBLE:
        paired_buttons.append(
            InlineKeyboardButton(text=settings.ACTIVATE_BUTTON_TEXT, callback_data="activate_button")
        )

    paired_buttons.append(
        InlineKeyboardButton(
            text=texts.t("MENU_INFO", "ℹ️ Инфо"),
            callback_data="menu_info",
        )
    )

    if settings.is_language_selection_enabled():
        paired_buttons.append(
            InlineKeyboardButton(text=texts.MENU_LANGUAGE, callback_data="menu_language")
        )

    for i in range(0, len(paired_buttons), 2):
        row = paired_buttons[i : i + 2]
        keyboard.append(row)

    if settings.DEBUG:
        print(f"DEBUG KEYBOARD: is_admin={is_admin}, добавляем админ кнопку: {is_admin}")

    if is_admin:
        if settings.DEBUG:
            print("DEBUG KEYBOARD: Админ кнопка ДОБАВЛЕНА!")
        keyboard.append([
            InlineKeyboardButton(text=texts.MENU_ADMIN, callback_data="admin_panel")
        ])
    else:
        if settings.DEBUG:
            print("DEBUG KEYBOARD: Админ кнопка НЕ добавлена")
    # Moderator access (limited support panel)
    if (not is_admin) and is_moderator:
        keyboard.append([
            InlineKeyboardButton(text="🧑‍⚖️ Модерация", callback_data="moderator_panel")
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_info_menu_keyboard(
    language: str = DEFAULT_LANGUAGE,
    show_privacy_policy: bool = False,
    show_public_offer: bool = False,
    show_faq: bool = False,
    show_promo_groups: bool = False,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    buttons: List[List[InlineKeyboardButton]] = []

    if show_faq:
        buttons.append([
            InlineKeyboardButton(
                text=texts.t("MENU_FAQ", "❓ FAQ"),
                callback_data="menu_faq",
            )
        ])

    if show_promo_groups:
        buttons.append([
            InlineKeyboardButton(
                text=texts.t("MENU_PROMO_GROUPS_INFO", "🎯 Промогруппы"),
                callback_data="menu_info_promo_groups",
            )
        ])

    if show_privacy_policy:
        buttons.append([
            InlineKeyboardButton(
                text=texts.t("MENU_PRIVACY_POLICY", "🛡️ Политика конф."),
                callback_data="menu_privacy_policy",
            )
        ])

    if show_public_offer:
        buttons.append([
            InlineKeyboardButton(
                text=texts.t("MENU_PUBLIC_OFFER", "📄 Оферта"),
                callback_data="menu_public_offer",
            )
        ])

    buttons.append([
        InlineKeyboardButton(text=texts.MENU_RULES, callback_data="menu_rules")
    ])

    server_status_mode = settings.get_server_status_mode()
    server_status_text = texts.t("MENU_SERVER_STATUS", "📊 Статус серверов")

    if server_status_mode == "external_link":
        status_url = settings.get_server_status_external_url()
        if status_url:
            buttons.append([InlineKeyboardButton(text=server_status_text, url=status_url)])
    elif server_status_mode == "external_link_miniapp":
        status_url = settings.get_server_status_external_url()
        if status_url:
            buttons.append([
                InlineKeyboardButton(
                    text=server_status_text,
                    web_app=types.WebAppInfo(url=status_url),
                )
            ])
    elif server_status_mode == "xray":
        buttons.append([
            InlineKeyboardButton(
                text=server_status_text,
                callback_data="menu_server_status",
            )
        ])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_happ_download_button_row(texts) -> Optional[List[InlineKeyboardButton]]:
    if not settings.is_happ_download_button_enabled():
        return None

    return [
        InlineKeyboardButton(
            text=texts.t("HAPP_DOWNLOAD_BUTTON", "⬇️ Скачать Happ"),
            callback_data="subscription_happ_download"
        )
    ]


def get_happ_cryptolink_keyboard(
    subscription_link: str,
    language: str = DEFAULT_LANGUAGE,
    redirect_link: Optional[str] = None,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    final_redirect_link = redirect_link or get_happ_cryptolink_redirect_link(subscription_link)

    buttons: List[List[InlineKeyboardButton]] = []

    if final_redirect_link:
        buttons.append([
            InlineKeyboardButton(
                text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                url=final_redirect_link,
            )
        ])

    buttons.extend([
        [
            InlineKeyboardButton(
                text=texts.t("HAPP_PLATFORM_IOS", "🍎 iOS"),
                callback_data="happ_download_ios",
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("HAPP_PLATFORM_ANDROID", "🤖 Android"),
                callback_data="happ_download_android",
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("HAPP_PLATFORM_MACOS", "🖥️ Mac OS"),
                callback_data="happ_download_macos",
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("HAPP_PLATFORM_WINDOWS", "💻 Windows"),
                callback_data="happ_download_windows",
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("BACK_TO_MAIN_MENU_BUTTON", "⬅️ В главное меню"),
                callback_data="back_to_menu",
            )
        ],
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_happ_download_platform_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t("HAPP_PLATFORM_IOS", "🍎 iOS"), callback_data="happ_download_ios")],
        [InlineKeyboardButton(text=texts.t("HAPP_PLATFORM_ANDROID", "🤖 Android"), callback_data="happ_download_android")],
        [InlineKeyboardButton(text=texts.t("HAPP_PLATFORM_MACOS", "🖥️ Mac OS"), callback_data="happ_download_macos")],
        [InlineKeyboardButton(text=texts.t("HAPP_PLATFORM_WINDOWS", "💻 Windows"), callback_data="happ_download_windows")],
        [InlineKeyboardButton(text=texts.BACK, callback_data="happ_download_close")],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_happ_download_link_keyboard(language: str, link: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t("HAPP_DOWNLOAD_OPEN_LINK", "🔗 Открыть ссылку"), url=link)],
        [InlineKeyboardButton(text=texts.BACK, callback_data="happ_download_back")],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_back_keyboard(language: str = DEFAULT_LANGUAGE, callback_data: str = "back_to_menu") -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BACK, callback_data=callback_data)]
    ])


def get_server_status_keyboard(
    language: str,
    current_page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=texts.t("SERVER_STATUS_REFRESH", "🔄 Обновить"),
                callback_data=f"server_status_page:{current_page}",
            )
        ]
    ]

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []

        if current_page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("SERVER_STATUS_PREV_PAGE", "⬅️ Назад"),
                    callback_data=f"server_status_page:{current_page - 1}",
                )
            )

        if current_page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("SERVER_STATUS_NEXT_PAGE", "Вперед ➡️"),
                    callback_data=f"server_status_page:{current_page + 1}",
                )
            )

        if nav_row:
            keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_insufficient_balance_keyboard(
    language: str = DEFAULT_LANGUAGE,
    resume_callback: str | None = None,
    amount_kopeks: int | None = None,
    has_saved_cart: bool = False,  # Новый параметр для указания наличия сохраненной корзины
) -> InlineKeyboardMarkup:

    texts = get_texts(language)
    keyboard = get_payment_methods_keyboard(amount_kopeks or 0, language)

    back_row_index: int | None = None

    if keyboard.inline_keyboard:
        last_row = keyboard.inline_keyboard[-1]
        if (
            len(last_row) == 1
            and isinstance(last_row[0], InlineKeyboardButton)
            and last_row[0].callback_data in {"menu_balance", "back_to_menu"}
        ):
            keyboard.inline_keyboard[-1][0] = InlineKeyboardButton(
                text=texts.t("PAYMENT_RETURN_HOME_BUTTON", "🏠 На главную"),
                callback_data="back_to_menu",
            )
            back_row_index = len(keyboard.inline_keyboard) - 1

    # Если есть сохраненная корзина, добавляем кнопку возврата к оформлению
    if has_saved_cart:
        return_row = [
            InlineKeyboardButton(
                text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                callback_data="return_to_saved_cart",
            )
        ]
        insert_index = back_row_index if back_row_index is not None else len(keyboard.inline_keyboard)
        keyboard.inline_keyboard.insert(insert_index, return_row)
    elif resume_callback:
        return_row = [
            InlineKeyboardButton(
                text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                callback_data=resume_callback,
            )
        ]
        insert_index = back_row_index if back_row_index is not None else len(keyboard.inline_keyboard)
        keyboard.inline_keyboard.insert(insert_index, return_row)

    return keyboard


def get_subscription_keyboard(
    language: str = DEFAULT_LANGUAGE, 
    has_subscription: bool = False, 
    is_trial: bool = False,
    subscription=None
) -> InlineKeyboardMarkup:
    from app.config import settings 
    
    texts = get_texts(language)
    keyboard = []
    has_direct_payment_methods = False

    if has_subscription:
        subscription_link = get_display_subscription_link(subscription) if subscription else None
        if subscription_link:
            connect_mode = settings.CONNECT_BUTTON_MODE

            if connect_mode == "miniapp_subscription":
                keyboard.append([
                    InlineKeyboardButton(
                        text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                        web_app=types.WebAppInfo(url=subscription_link)
                    )
                ])
            elif connect_mode == "miniapp_custom":
                if settings.MINIAPP_CUSTOM_URL:
                    keyboard.append([
                        InlineKeyboardButton(
                            text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                            web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL)
                        )
                    ])
                else:
                    keyboard.append([
                        InlineKeyboardButton(text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"), callback_data="subscription_connect")
                    ])
            elif connect_mode == "link":
                keyboard.append([
                    InlineKeyboardButton(text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"), url=subscription_link)
                ])
            elif connect_mode == "happ_cryptolink":
                keyboard.append([
                    InlineKeyboardButton(
                        text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                        callback_data="open_subscription_link",
                    )
                ])
            else:
                keyboard.append([
                    InlineKeyboardButton(text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"), callback_data="subscription_connect")
                ])
        elif settings.CONNECT_BUTTON_MODE == "miniapp_custom":
            keyboard.append([
                InlineKeyboardButton(
                    text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
                    web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL)
                )
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"), callback_data="subscription_connect")
            ])

        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            keyboard.append(happ_row)

        if is_trial:
            keyboard.append([
                InlineKeyboardButton(text=texts.MENU_BUY_SUBSCRIPTION, callback_data="subscription_upgrade")
            ])
        else:
            # Проверяем, является ли тариф суточным
            tariff = getattr(subscription, 'tariff', None) if subscription else None
            is_daily_tariff = tariff and getattr(tariff, 'is_daily', False)

            if is_daily_tariff:
                # Для суточного тарифа показываем кнопку паузы/возобновления
                is_paused = getattr(subscription, 'is_daily_paused', False)
                if is_paused:
                    pause_text = texts.t("RESUME_DAILY_BUTTON", "▶️ Возобновить подписку")
                else:
                    pause_text = texts.t("PAUSE_DAILY_BUTTON", "⏸️ Приостановить подписку")
                keyboard.append([
                    InlineKeyboardButton(text=pause_text, callback_data="toggle_daily_subscription_pause")
                ])
            else:
                # Для обычного тарифа: [Продлить] [Автоплатеж]
                keyboard.append([
                    InlineKeyboardButton(text=texts.MENU_EXTEND_SUBSCRIPTION, callback_data="subscription_extend"),
                    InlineKeyboardButton(
                        text=texts.t("AUTOPAY_BUTTON", "💳 Автоплатеж"),
                        callback_data="subscription_autopay",
                    )
                ])

            # Ряд: [Настройки] [Тариф] (если режим тарифов)
            settings_row = [
                InlineKeyboardButton(
                    text=texts.t("SUBSCRIPTION_SETTINGS_BUTTON", "⚙️ Настройки"),
                    callback_data="subscription_settings",
                )
            ]
            if settings.is_tariffs_mode() and subscription:
                # Для суточных тарифов переходим на список тарифов, для обычных - мгновенное переключение
                tariff_callback = "tariff_switch" if is_daily_tariff else "instant_switch"
                settings_row.append(
                    InlineKeyboardButton(
                        text=texts.t("CHANGE_TARIFF_BUTTON", "📦 Тариф"),
                        callback_data=tariff_callback
                    )
                )
            keyboard.append(settings_row)

            # Кнопка докупки трафика для платных подписок
            # В режиме тарифов проверяем tariff_id, в классическом - глобальные настройки
            show_traffic_topup = False
            if subscription and (subscription.traffic_limit_gb or 0) > 0:
                if settings.is_tariffs_mode() and getattr(subscription, 'tariff_id', None):
                    show_traffic_topup = True
                elif settings.is_traffic_topup_enabled() and not settings.is_traffic_topup_blocked():
                    show_traffic_topup = True

            if show_traffic_topup:
                keyboard.append([
                    InlineKeyboardButton(
                        text=texts.t("BUY_TRAFFIC_BUTTON", "📈 Докупить трафик"),
                        callback_data="buy_traffic"
                    )
                ])

    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_payment_methods_keyboard_with_cart(
    language: str = "ru",
    amount_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = get_payment_methods_keyboard(amount_kopeks, language)
    
    # Добавляем кнопку "Очистить корзину"
    keyboard.inline_keyboard.append([
        InlineKeyboardButton(
            text="🗑️ Очистить корзину и вернуться",
            callback_data="clear_saved_cart"
        )
    ])
    
    # Добавляем кнопку возврата к оформлению подписки
    keyboard.inline_keyboard.insert(-1, [  # Вставляем перед кнопкой "назад"
        InlineKeyboardButton(
            text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
            callback_data="return_to_saved_cart"
        )
    ])
    
    return keyboard

def get_subscription_confirm_keyboard_with_cart(language: str = "ru") -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить покупку",
            callback_data="subscription_confirm"
        )],
        [InlineKeyboardButton(
            text="🗑️ Очистить корзину",
            callback_data="clear_saved_cart"
        )],
        [InlineKeyboardButton(
            text=texts.BACK,
            callback_data="subscription_config_back"  # Изменили на возврат к настройке
        )]
    ])

def get_insufficient_balance_keyboard_with_cart(
    language: str = "ru",
    amount_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    # Используем обновленную версию с флагом has_saved_cart=True
    keyboard = get_insufficient_balance_keyboard(
        language,
        amount_kopeks=amount_kopeks,
        has_saved_cart=True,
    )

    # Добавляем кнопку очистки корзины в начало
    keyboard.inline_keyboard.insert(
        0,
        [
            InlineKeyboardButton(
                text="🗑️ Очистить корзину и вернуться",
                callback_data="clear_saved_cart",
            )
        ],
    )

    return keyboard

def get_trial_keyboard(language: str = "ru") -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=texts.t("TRIAL_ACTIVATE_BUTTON", "🎁 Активировать"), callback_data="trial_activate"),
            InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")
        ]
    ])


def get_subscription_period_keyboard(
    language: str = DEFAULT_LANGUAGE,
    user: Optional[User] = None
) -> InlineKeyboardMarkup:
    """
    Generate subscription period selection keyboard with personalized pricing.

    Args:
        language: User's language code
        user: User object for personalized discounts (None = default discounts)

    Returns:
        InlineKeyboardMarkup with period buttons showing personalized prices
    """
    from app.utils.price_display import calculate_user_price

    texts = get_texts(language)
    keyboard = []

    available_periods = settings.get_available_subscription_periods()

    for days in available_periods:
        # Get base price for this period
        base_price = PERIOD_PRICES.get(days, 0)

        # Calculate personalized price with user's discounts
        price_info = calculate_user_price(user, base_price, days, "period")

        # Format period description
        period_display = format_period_description(days, language)

        # Format button text with discount display
        button_text = format_price_button(
            period_label=period_display,
            price_info=price_info,
            format_price_func=texts.format_price,
            emphasize=False,
            add_exclamation=False
        )

        keyboard.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"period_{days}"
            )
        ])

    # Кнопка "Простая покупка" была убрана из выбора периода подписки

    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_traffic_packages_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    import logging
    logger = logging.getLogger(__name__)

    from app.config import settings

    if settings.is_traffic_topup_blocked():
        return get_back_keyboard(language)
    
    logger.info(f"🔍 RAW CONFIG: '{settings.TRAFFIC_PACKAGES_CONFIG}'")
    
    all_packages = settings.get_traffic_packages()
    logger.info(f"🔍 ALL PACKAGES: {all_packages}")
    
    enabled_packages = [pkg for pkg in all_packages if pkg['enabled']]
    disabled_packages = [pkg for pkg in all_packages if not pkg['enabled']]
    
    logger.info(f"🔍 ENABLED: {len(enabled_packages)} packages")
    logger.info(f"🔍 DISABLED: {len(disabled_packages)} packages")
    
    for pkg in disabled_packages:
        logger.info(f"🔍 DISABLED PACKAGE: {pkg['gb']}GB = {pkg['price']} kopeks, enabled={pkg['enabled']}")
    
    texts = get_texts(language)
    keyboard = []
    
    traffic_packages = settings.get_traffic_packages()
    
    for package in traffic_packages:
        gb = package["gb"]
        price = package["price"]  
        enabled = package["enabled"]
        
        if not enabled:
            continue
        
        if gb == 0:
            text = f"♾️ Безлимит - {settings.format_price(package['price'])}"
        else:
            text = f"📊 {gb} ГБ - {settings.format_price(package['price'])}"
        
        keyboard.append([
            InlineKeyboardButton(text=text, callback_data=f"traffic_{gb}")
        ])

    if not keyboard:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("TRAFFIC_PACKAGES_NOT_CONFIGURED", "⚠️ Пакеты трафика не настроены"), 
                callback_data="no_traffic_packages"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="subscription_config_back")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_countries_keyboard(countries: List[dict], selected: List[str], language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    for country in countries:
        if not country.get('is_available', True):
            continue 
            
        emoji = "✅" if country['uuid'] in selected else "⚪"
        
        if country['price_kopeks'] > 0:
            price_text = f" (+{texts.format_price(country['price_kopeks'])})"
        else:
            price_text = " (Бесплатно)"
        
        keyboard.append([
            InlineKeyboardButton(
                text=f"{emoji} {country['name']}{price_text}",
                callback_data=f"country_{country['uuid']}"
            )
        ])
    
    if not keyboard:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("NO_SERVERS_AVAILABLE", "❌ Нет доступных серверов"),
                callback_data="no_servers"
            )
        ])
    
    keyboard.extend([
        [InlineKeyboardButton(text=texts.t("CONTINUE_BUTTON", "✅ Продолжить"), callback_data="countries_continue")],
        [InlineKeyboardButton(text=texts.BACK, callback_data="subscription_config_back")]
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_devices_keyboard(current: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    start_devices = settings.DEFAULT_DEVICE_LIMIT
    max_devices = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else 50
    end_devices = min(max_devices + 1, start_devices + 10)
    
    buttons = []
    
    for devices in range(start_devices, end_devices): 
        price = max(0, devices - settings.DEFAULT_DEVICE_LIMIT) * settings.PRICE_PER_DEVICE
        price_text = f" (+{texts.format_price(price)})" if price > 0 else " (вкл.)"
        emoji = "✅" if devices == current else "⚪"
        
        button_text = f"{emoji} {devices}{price_text}"
        
        buttons.append(
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"devices_{devices}"
            )
        )
    
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            keyboard.append([buttons[i], buttons[i + 1]])
        else:
            keyboard.append([buttons[i]])
    
    keyboard.extend([
        [InlineKeyboardButton(text=texts.t("CONTINUE_BUTTON", "✅ Продолжить"), callback_data="devices_continue")],
        [InlineKeyboardButton(text=texts.BACK, callback_data="subscription_config_back")]
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def _get_device_declension(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "устройство"
    elif count % 10 in [2, 3, 4] and count % 100 not in [12, 13, 14]:
        return "устройства"
    else:
        return "устройств"

def get_subscription_confirm_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=texts.CONFIRM, callback_data="subscription_confirm"),
            InlineKeyboardButton(text=texts.CANCEL, callback_data="subscription_cancel")
        ]
    ])


def get_balance_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    
    keyboard = [
        [
            InlineKeyboardButton(text=texts.BALANCE_HISTORY, callback_data="balance_history"),
            InlineKeyboardButton(text=texts.BALANCE_TOP_UP, callback_data="balance_topup")
        ],
        [
            InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")
        ]
    ]
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_payment_methods_keyboard(amount_kopeks: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    has_direct_payment_methods = False

    amount_kopeks = max(0, int(amount_kopeks or 0))

    def _build_callback(method: str) -> str:
        if amount_kopeks > 0:
            return f"topup_amount|{method}|{amount_kopeks}"
        return f"topup_{method}"

    if settings.TELEGRAM_STARS_ENABLED:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_TELEGRAM_STARS", "⭐ Telegram Stars"),
                callback_data=_build_callback("stars")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_yookassa_enabled():
        if settings.YOOKASSA_SBP_ENABLED:
            keyboard.append([
                InlineKeyboardButton(
                    text=texts.t("PAYMENT_SBP_YOOKASSA", "🏦 Оплатить по СБП (YooKassa)"),
                    callback_data=_build_callback("yookassa_sbp"),
                )
            ])
            has_direct_payment_methods = True

        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_CARD_YOOKASSA", "💳 Банковская карта (YooKassa)"),
                callback_data=_build_callback("yookassa"),
            )
        ])
        has_direct_payment_methods = True

    if settings.TRIBUTE_ENABLED:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_CARD_TRIBUTE", "💳 Банковская карта (Tribute)"),
                callback_data=_build_callback("tribute")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_mulenpay_enabled():
        mulenpay_name = settings.get_mulenpay_display_name()
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t(
                    "PAYMENT_CARD_MULENPAY",
                    "💳 Банковская карта ({mulenpay_name})",
                ).format(mulenpay_name=mulenpay_name),
                callback_data=_build_callback("mulenpay")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_wata_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_CARD_WATA", "💳 Банковская карта (WATA)"),
                callback_data=_build_callback("wata")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_pal24_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_CARD_PAL24", "🏦 СБП (PayPalych)"),
                callback_data=_build_callback("pal24")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_platega_enabled() and settings.get_platega_active_methods():
        platega_name = settings.get_platega_display_name()
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_PLATEGA", f"💳 {platega_name}"),
                callback_data=_build_callback("platega"),
            )
        ])
        has_direct_payment_methods = True

    if settings.is_cryptobot_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_CRYPTOBOT", "🪙 Криптовалюта (CryptoBot)"),
                callback_data=_build_callback("cryptobot")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_heleket_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_HELEKET", "🪙 Криптовалюта (Heleket)"),
                callback_data=_build_callback("heleket")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_cloudpayments_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_CLOUDPAYMENTS", "💳 Банковская карта (CloudPayments)"),
                callback_data=_build_callback("cloudpayments")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_freekassa_enabled():
        freekassa_name = settings.get_freekassa_display_name()
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_FREEKASSA", f"💳 {freekassa_name}"),
                callback_data=_build_callback("freekassa")
            )
        ])
        has_direct_payment_methods = True

    if settings.is_support_topup_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENT_VIA_SUPPORT", "🛠️ Через поддержку"),
                callback_data="topup_support"
            )
        ])

    if not keyboard:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("PAYMENTS_TEMPORARILY_UNAVAILABLE", "⚠️ Способы оплаты временно недоступны"),
                callback_data="payment_methods_unavailable"
            )
        ])
    elif not has_direct_payment_methods and settings.is_support_topup_enabled():
        keyboard.insert(0, [
            InlineKeyboardButton(
                text=texts.t("PAYMENTS_TEMPORARILY_UNAVAILABLE", "⚠️ Способы оплаты временно недоступны"),
                callback_data="payment_methods_unavailable"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="menu_balance")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_yookassa_payment_keyboard(
    payment_id: str, 
    amount_kopeks: int, 
    confirmation_url: str,
    language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("PAY_NOW_BUTTON", "💳 Оплатить"),
                url=confirmation_url
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("CHECK_STATUS_BUTTON", "📊 Проверить статус"),
                callback_data=f"check_yookassa_status_{payment_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("MY_BALANCE_BUTTON", "💰 Мой баланс"),
                callback_data="menu_balance"
            )
        ]
    ])

def get_autopay_notification_keyboard(subscription_id: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            build_miniapp_or_callback_button(
                text=texts.t("TOPUP_BALANCE_BUTTON", "💳 Пополнить баланс"),
                callback_data="balance_topup"
            )
        ],
        [
            build_miniapp_or_callback_button(
                text=texts.t("MY_SUBSCRIPTION_BUTTON", "📱 Моя подписка"),
                callback_data="menu_subscription"
            )
        ]
    ])

def get_subscription_expiring_keyboard(subscription_id: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            build_miniapp_or_callback_button(
                text=texts.MENU_EXTEND_SUBSCRIPTION,
                callback_data="subscription_extend"
            )
        ],
        [
            build_miniapp_or_callback_button(
                text=texts.t("TOPUP_BALANCE_BUTTON", "💳 Пополнить баланс"),
                callback_data="balance_topup"
            )
        ],
        [
            build_miniapp_or_callback_button(
                text=texts.t("MY_SUBSCRIPTION_BUTTON", "📱 Моя подписка"),
                callback_data="menu_subscription"
            )
        ]
    ])

def get_referral_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    keyboard = [
        [
            InlineKeyboardButton(
                text=texts.t("CREATE_INVITE_BUTTON", "📝 Создать приглашение"),
                callback_data="referral_create_invite"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("SHOW_QR_BUTTON", "📱 Показать QR код"),
                callback_data="referral_show_qr"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("REFERRAL_LIST_BUTTON", "👥 Список рефералов"),
                callback_data="referral_list"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("REFERRAL_ANALYTICS_BUTTON", "📊 Аналитика"),
                callback_data="referral_analytics"
            )
        ],
    ]

    # Добавляем кнопку вывода, если включена
    if settings.is_referral_withdrawal_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("REFERRAL_WITHDRAWAL_BUTTON", "💸 Запросить вывод"),
                callback_data="referral_withdrawal"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text=texts.BACK,
            callback_data="back_to_menu"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_support_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    try:
        from app.services.support_settings_service import SupportSettingsService
        tickets_enabled = SupportSettingsService.is_tickets_enabled()
        contact_enabled = SupportSettingsService.is_contact_enabled()
    except Exception:
        tickets_enabled = True
        contact_enabled = True
    rows: list[list[InlineKeyboardButton]] = []
    # Tickets
    if tickets_enabled:
        rows.append([
            InlineKeyboardButton(
                text=texts.t("CREATE_TICKET_BUTTON", "🎫 Создать тикет"),
                callback_data="create_ticket"
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text=texts.t("MY_TICKETS_BUTTON", "📋 Мои тикеты"),
                callback_data="my_tickets"
            )
        ])
    # Direct contact
    if contact_enabled and settings.get_support_contact_url():
        rows.append([
            InlineKeyboardButton(
                text=texts.t("CONTACT_SUPPORT_BUTTON", "💬 Связаться с поддержкой"),
                url=settings.get_support_contact_url() or "https://t.me/"
            )
        ])
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_pagination_keyboard(
    current_page: int,
    total_pages: int,
    callback_prefix: str,
    language: str = DEFAULT_LANGUAGE
) -> List[List[InlineKeyboardButton]]:
    texts = get_texts(language)
    keyboard = []
    
    if total_pages > 1:
        row = []
        
        if current_page > 1:
            row.append(InlineKeyboardButton(
                text=texts.t("PAGINATION_PREV", "⬅️"),
                callback_data=f"{callback_prefix}_page_{current_page - 1}"
            ))
        
        row.append(InlineKeyboardButton(
            text=f"{current_page}/{total_pages}",
            callback_data="current_page"
        ))
        
        if current_page < total_pages:
            row.append(InlineKeyboardButton(
                text=texts.t("PAGINATION_NEXT", "➡️"),
                callback_data=f"{callback_prefix}_page_{current_page + 1}"
            ))
        
        keyboard.append(row)
    
    return keyboard

def get_confirmation_keyboard(
    confirm_data: str,
    cancel_data: str = "cancel",
    language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=texts.YES, callback_data=confirm_data),
            InlineKeyboardButton(text=texts.NO, callback_data=cancel_data)
        ]
    ])


def get_autopay_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=texts.t("ENABLE_BUTTON", "✅ Включить"), callback_data="autopay_enable"),
            InlineKeyboardButton(text=texts.t("DISABLE_BUTTON", "❌ Выключить"), callback_data="autopay_disable")
        ],
        [
            InlineKeyboardButton(text=texts.t("AUTOPAY_SET_DAYS_BUTTON", "⚙️ Настроить дни"), callback_data="autopay_set_days")
        ],
        [
            InlineKeyboardButton(text=texts.BACK, callback_data="menu_subscription")
        ]
    ])


def get_autopay_days_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    for days in [1, 3, 7, 14]:
        keyboard.append([
            InlineKeyboardButton(
                text=f"{days} {_get_days_word(days)}",
                callback_data=f"autopay_days_{days}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="subscription_autopay")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _get_days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    if 2 <= days % 10 <= 4 and not (12 <= days % 100 <= 14):
        return "дня"
    return "дней"



# Deprecated: get_extend_subscription_keyboard() was removed.
# Use get_extend_subscription_keyboard_with_prices() instead for personalized pricing.


def get_add_traffic_keyboard(
    language: str = DEFAULT_LANGUAGE,
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
) -> InlineKeyboardMarkup:
    from app.utils.pricing_utils import get_remaining_months
    from app.config import settings
    texts = get_texts(language)
    
    months_multiplier = 1
    period_text = ""
    if subscription_end_date:
        months_multiplier = get_remaining_months(subscription_end_date)
        if months_multiplier > 1:
            period_text = f" (за {months_multiplier} мес)"
    
    packages = settings.get_traffic_topup_packages()
    enabled_packages = [pkg for pkg in packages if pkg['enabled']]
    
    if not enabled_packages:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=texts.t("NO_TRAFFIC_PACKAGES", "❌ Нет доступных пакетов"),
                callback_data="no_traffic_packages"
            )],
            [InlineKeyboardButton(
                text=texts.BACK,
                callback_data="menu_subscription"
            )]
        ])
    
    buttons = []
    
    for package in enabled_packages:
        gb = package['gb']
        price_per_month = package['price']
        discounted_per_month, discount_per_month = apply_percentage_discount(
            price_per_month,
            discount_percent,
        )
        total_price = discounted_per_month * months_multiplier
        total_discount = discount_per_month * months_multiplier

        if gb == 0:
            if language == "ru":
                text = f"♾️ Безлимитный трафик - {total_price//100} ₽{period_text}"
            else:
                text = f"♾️ Unlimited traffic - {total_price//100} ₽{period_text}"
        else:
            if language == "ru":
                text = f"📊 +{gb} ГБ трафика - {total_price//100} ₽{period_text}"
            else:
                text = f"📊 +{gb} GB traffic - {total_price//100} ₽{period_text}"

        if discount_percent > 0 and total_discount > 0:
            text += f" (скидка {discount_percent}%: -{total_discount//100}₽)"

        buttons.append([
            InlineKeyboardButton(text=text, callback_data=f"add_traffic_{gb}")
        ])
    
    buttons.append([
        InlineKeyboardButton(
            text=texts.BACK,
            callback_data="menu_subscription"
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_add_traffic_keyboard_from_tariff(
    language: str,
    packages: dict,  # {gb: price_kopeks}
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
) -> InlineKeyboardMarkup:
    """
    Клавиатура для докупки трафика из настроек тарифа.

    Args:
        language: Язык интерфейса
        packages: Словарь {ГБ: цена_в_копейках} из тарифа
        subscription_end_date: Дата окончания подписки для расчета цены
        discount_percent: Процент скидки
    """
    from app.utils.pricing_utils import get_remaining_months

    texts = get_texts(language)

    months_multiplier = 1
    period_text = ""
    if subscription_end_date:
        months_multiplier = get_remaining_months(subscription_end_date)
        if months_multiplier > 1:
            period_text = f" (за {months_multiplier} мес)"

    if not packages:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=texts.t("NO_TRAFFIC_PACKAGES", "❌ Нет доступных пакетов"),
                callback_data="no_traffic_packages"
            )],
            [InlineKeyboardButton(
                text=texts.BACK,
                callback_data="menu_subscription"
            )]
        ])

    buttons = []

    # Сортируем пакеты по размеру
    sorted_packages = sorted(packages.items(), key=lambda x: x[0])

    for gb, price_per_month in sorted_packages:
        discounted_per_month, discount_per_month = apply_percentage_discount(
            price_per_month,
            discount_percent,
        )
        total_price = discounted_per_month * months_multiplier
        total_discount = discount_per_month * months_multiplier

        if language == "ru":
            text = f"📊 +{gb} ГБ трафика - {total_price // 100} ₽{period_text}"
        else:
            text = f"📊 +{gb} GB traffic - {total_price // 100} ₽{period_text}"

        if discount_percent > 0 and total_discount > 0:
            text += f" (скидка {discount_percent}%: -{total_discount // 100}₽)"

        buttons.append([
            InlineKeyboardButton(text=text, callback_data=f"add_traffic_{gb}")
        ])

    buttons.append([
        InlineKeyboardButton(
            text=texts.BACK,
            callback_data="menu_subscription"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_change_devices_keyboard(
    current_devices: int,
    language: str = DEFAULT_LANGUAGE,
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
    tariff=None,  # Тариф для цены за устройство
) -> InlineKeyboardMarkup:
    from app.utils.pricing_utils import get_remaining_months
    from app.config import settings
    texts = get_texts(language)

    months_multiplier = 1
    period_text = ""
    if subscription_end_date:
        months_multiplier = get_remaining_months(subscription_end_date)
        if months_multiplier > 1:
            period_text = f" (за {months_multiplier} мес)"

    # Используем цену из тарифа если есть, иначе глобальную настройку
    tariff_device_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
    if tariff and tariff_device_price:
        device_price_per_month = tariff_device_price
        # Для тарифов все устройства платные (нет бесплатного лимита)
        default_device_limit = 0
    else:
        device_price_per_month = settings.PRICE_PER_DEVICE
        default_device_limit = settings.DEFAULT_DEVICE_LIMIT

    buttons = []

    min_devices = 1
    max_devices = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else 20

    start_range = max(1, min(current_devices - 3, max_devices - 6))
    end_range = min(max_devices + 1, max(current_devices + 4, 7))

    for devices_count in range(start_range, end_range):
        if devices_count == current_devices:
            emoji = "✅"
            action_text = " (текущее)"
            price_text = ""
        elif devices_count > current_devices:
            emoji = "➕"
            additional_devices = devices_count - current_devices

            current_chargeable = max(0, current_devices - default_device_limit)
            new_chargeable = max(0, devices_count - default_device_limit)
            chargeable_devices = new_chargeable - current_chargeable

            if chargeable_devices > 0:
                price_per_month = chargeable_devices * device_price_per_month
                discounted_per_month, discount_per_month = apply_percentage_discount(
                    price_per_month,
                    discount_percent,
                )
                total_price = discounted_per_month * months_multiplier
                price_text = f" (+{total_price//100}₽{period_text})"
                if discount_percent > 0 and discount_per_month * months_multiplier > 0:
                    price_text += (
                        f" (скидка {discount_percent}%:"
                        f" -{(discount_per_month * months_multiplier)//100}₽)"
                    )
                action_text = ""
            else:
                price_text = " (бесплатно)"
                action_text = ""
        else:
            emoji = "➖"
            action_text = ""
            price_text = " (без возврата)"

        button_text = f"{emoji} {devices_count} устр.{action_text}{price_text}"

        buttons.append([
            InlineKeyboardButton(text=button_text, callback_data=f"change_devices_{devices_count}")
        ])

    if current_devices < start_range or current_devices >= end_range:
        current_button = f"✅ {current_devices} устр. (текущее)"
        buttons.insert(0, [
            InlineKeyboardButton(text=current_button, callback_data=f"change_devices_{current_devices}")
        ])

    buttons.append([
        InlineKeyboardButton(
            text=texts.BACK,
            callback_data="subscription_settings"
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_confirm_change_devices_keyboard(new_devices_count: int, price: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("CONFIRM_CHANGE_BUTTON", "✅ Подтвердить изменение"),
                callback_data=f"confirm_change_devices_{new_devices_count}_{price}"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.CANCEL,
                callback_data="subscription_settings"
            )
        ]
    ])


def get_reset_traffic_confirm_keyboard(
    price_kopeks: int, 
    language: str = DEFAULT_LANGUAGE,
    has_enough_balance: bool = True,
    missing_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        return get_back_keyboard(language)
    
    texts = get_texts(language)
    buttons = []
    
    if has_enough_balance:
        # Достаточно средств - показываем кнопку сброса
        buttons.append([
            InlineKeyboardButton(
                text=f"✅ Сбросить за {settings.format_price(price_kopeks)}", 
                callback_data="confirm_reset_traffic"
            )
        ])
    else:
        # Не хватает средств - показываем кнопку пополнения
        buttons.append([
            InlineKeyboardButton(
                text=texts.t("TOPUP_BALANCE_BUTTON", "💳 Пополнить баланс"),
                callback_data=f"topup_amount_{missing_kopeks}"
            )
        ])
    
    buttons.append([
        InlineKeyboardButton(
            text=texts.BACK,
            callback_data="subscription_settings",
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_manage_countries_keyboard(
    countries: List[dict],
    selected: List[str],
    current_subscription_countries: List[str],
    language: str = DEFAULT_LANGUAGE,
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
) -> InlineKeyboardMarkup:
    from app.utils.pricing_utils import get_remaining_months

    texts = get_texts(language)

    months_multiplier = 1
    if subscription_end_date:
        months_multiplier = get_remaining_months(subscription_end_date)
        logger.info(f"🔍 Расчет для управления странами: осталось {months_multiplier} месяцев до {subscription_end_date}")
    
    buttons = []
    total_cost = 0
    
    for country in countries:
        if not country.get('is_available', True):
            continue

        uuid = country['uuid']
        name = country['name']
        price_per_month = country['price_kopeks']

        discounted_per_month, discount_per_month = apply_percentage_discount(
            price_per_month,
            discount_percent,
        )

        if uuid in current_subscription_countries:
            if uuid in selected:
                icon = "✅"
            else:
                icon = "➖"
        else:
            if uuid in selected:
                icon = "➕"
                total_cost += discounted_per_month * months_multiplier
            else:
                icon = "⚪"

        if uuid not in current_subscription_countries and uuid in selected:
            total_price = discounted_per_month * months_multiplier
            if months_multiplier > 1:
                price_text = (
                    f" ({discounted_per_month//100}₽/мес × {months_multiplier} = {total_price//100}₽)"
                )
                logger.info(
                    "🔍 Сервер %s: %.2f₽/мес × %s мес = %.2f₽ (скидка %.2f₽)",
                    name,
                    discounted_per_month / 100,
                    months_multiplier,
                    total_price / 100,
                    (discount_per_month * months_multiplier) / 100,
                )
            else:
                price_text = f" ({total_price//100}₽)"
            if discount_percent > 0 and discount_per_month * months_multiplier > 0:
                price_text += (
                    f" (скидка {discount_percent}%:"
                    f" -{(discount_per_month * months_multiplier)//100}₽)"
                )
            display_name = f"{icon} {name}{price_text}"
        else:
            display_name = f"{icon} {name}"
        
        buttons.append([
            InlineKeyboardButton(
                text=display_name,
                callback_data=f"country_manage_{uuid}"
            )
        ])
    
    if total_cost > 0:
        apply_text = f"✅ Применить изменения ({total_cost//100} ₽)"
        logger.info(f"🔍 Общая стоимость новых серверов: {total_cost/100}₽")
    else:
        apply_text = "✅ Применить изменения"
    
    buttons.append([
        InlineKeyboardButton(text=apply_text, callback_data="countries_apply")
    ])
    
    buttons.append([
        InlineKeyboardButton(
            text=texts.BACK,
            callback_data="menu_subscription"
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_device_selection_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    from app.config import settings
    texts = get_texts(language)
    
    keyboard = [
        [
            InlineKeyboardButton(text=texts.t("DEVICE_GUIDE_IOS", "📱 iOS (iPhone/iPad)"), callback_data="device_guide_ios"),
            InlineKeyboardButton(text=texts.t("DEVICE_GUIDE_ANDROID", "🤖 Android"), callback_data="device_guide_android")
        ],
        [
            InlineKeyboardButton(text=texts.t("DEVICE_GUIDE_WINDOWS", "💻 Windows"), callback_data="device_guide_windows"),
            InlineKeyboardButton(text=texts.t("DEVICE_GUIDE_MAC", "🎯 macOS"), callback_data="device_guide_mac")
        ],
        [
            InlineKeyboardButton(text=texts.t("DEVICE_GUIDE_ANDROID_TV", "📺 Android TV"), callback_data="device_guide_tv"),
            InlineKeyboardButton(text=texts.t("DEVICE_GUIDE_APPLE_TV", "📺 Apple TV"), callback_data="device_guide_appletv")
        ]
    ]
    
    if settings.CONNECT_BUTTON_MODE == "guide":
        keyboard.append([
            InlineKeyboardButton(text=texts.t("SHOW_SUBSCRIPTION_LINK", "📋 Показать ссылку подписки"), callback_data="open_subscription_link")
        ])
    
    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="menu_subscription")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_connection_guide_keyboard(
    subscription_url: str,
    app: dict,
    device_type: str,
    language: str = DEFAULT_LANGUAGE,
    has_other_apps: bool = False,
) -> InlineKeyboardMarkup:
    from app.handlers.subscription import create_deep_link
    texts = get_texts(language)

    keyboard = []

    if 'installationStep' in app and 'buttons' in app['installationStep']:
        app_buttons = []
        for button in app['installationStep']['buttons']:
            button_text = _get_localized_value(button.get('buttonText'), language)
            button_link = button.get('buttonLink')

            if not button_text or not button_link:
                continue

            app_buttons.append(
                InlineKeyboardButton(text=f"📥 {button_text}", url=button_link)
            )
            if len(app_buttons) == 2:
                keyboard.append(app_buttons)
                app_buttons = []

        if app_buttons:
            keyboard.append(app_buttons)

    additional_before_buttons = _build_additional_buttons(
        app.get('additionalBeforeAddSubscriptionStep'),
        language,
    )

    for button in additional_before_buttons:
        keyboard.append([button])

    connect_link = create_deep_link(app, subscription_url)

    if connect_link:
        connect_button = InlineKeyboardButton(
            text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
            url=connect_link,
        )
    elif settings.is_happ_cryptolink_mode():
        connect_button = InlineKeyboardButton(
            text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
            callback_data="open_subscription_link",
        )
    else:
        connect_button = InlineKeyboardButton(
            text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
            url=subscription_url,
        )

    keyboard.append([connect_button])

    additional_after_buttons = _build_additional_buttons(
        app.get('additionalAfterAddSubscriptionStep'),
        language,
    )

    for button in additional_after_buttons:
        keyboard.append([button])

    if has_other_apps:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("OTHER_APPS_BUTTON", "📋 Другие приложения"),
                callback_data=f"app_list_{device_type}",
            )
        ])

    keyboard.extend([
        [
            InlineKeyboardButton(text=texts.t("CHOOSE_ANOTHER_DEVICE", "📱 Выбрать другое устройство"), callback_data="subscription_connect")
        ],
        [
            InlineKeyboardButton(text=texts.t("BACK_TO_SUBSCRIPTION", "⬅️ К подписке"), callback_data="menu_subscription")
        ]
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_app_selection_keyboard(
    device_type: str, 
    apps: list, 
    language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    for app in apps:
        app_name = app['name']
        if app.get('isFeatured', False):
            app_name = f"⭐ {app_name}"
        
        keyboard.append([
            InlineKeyboardButton(
                text=app_name, 
                callback_data=f"app_{device_type}_{app['id']}"
            )
        ])
    
    keyboard.extend([
        [
            InlineKeyboardButton(text=texts.t("CHOOSE_ANOTHER_DEVICE", "📱 Выбрать другое устройство"), callback_data="subscription_connect")
        ],
        [
            InlineKeyboardButton(text=texts.t("BACK_TO_SUBSCRIPTION", "⬅️ К подписке"), callback_data="menu_subscription")
        ]
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_specific_app_keyboard(
    subscription_url: str,
    app: dict,
    device_type: str,
    language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    from app.handlers.subscription import create_deep_link
    texts = get_texts(language)

    keyboard = []

    if 'installationStep' in app and 'buttons' in app['installationStep']:
        app_buttons = []
        for button in app['installationStep']['buttons']:
            button_text = _get_localized_value(button.get('buttonText'), language)
            button_link = button.get('buttonLink')

            if not button_text or not button_link:
                continue

            app_buttons.append(
                InlineKeyboardButton(text=f"📥 {button_text}", url=button_link)
            )
            if len(app_buttons) == 2:
                keyboard.append(app_buttons)
                app_buttons = []

        if app_buttons:
            keyboard.append(app_buttons)

    additional_before_buttons = _build_additional_buttons(
        app.get('additionalBeforeAddSubscriptionStep'),
        language,
    )

    for button in additional_before_buttons:
        keyboard.append([button])

    connect_link = create_deep_link(app, subscription_url)

    if connect_link:
        connect_button = InlineKeyboardButton(
            text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
            url=connect_link,
        )
    elif settings.is_happ_cryptolink_mode():
        connect_button = InlineKeyboardButton(
            text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
            callback_data="open_subscription_link",
        )
    else:
        connect_button = InlineKeyboardButton(
            text=texts.t("CONNECT_BUTTON", "🔗 Подключиться"),
            url=subscription_url,
        )

    keyboard.append([connect_button])

    additional_after_buttons = _build_additional_buttons(
        app.get('additionalAfterAddSubscriptionStep'),
        language,
    )

    for button in additional_after_buttons:
        keyboard.append([button])

    keyboard.extend([
        [
            InlineKeyboardButton(text=texts.t("OTHER_APPS_BUTTON", "📋 Другие приложения"), callback_data=f"app_list_{device_type}")
        ],
        [
            InlineKeyboardButton(text=texts.t("CHOOSE_ANOTHER_DEVICE", "📱 Выбрать другое устройство"), callback_data="subscription_connect")
        ],
        [
            InlineKeyboardButton(text=texts.t("BACK_TO_SUBSCRIPTION", "⬅️ К подписке"), callback_data="menu_subscription")
        ]
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_extend_subscription_keyboard_with_prices(language: str, prices: dict) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    available_periods = settings.get_available_renewal_periods()

    for days in available_periods:
        if days not in prices:
            continue

        price_info = prices[days]

        if isinstance(price_info, dict):
            final_price = price_info.get("final")
            original_price = price_info.get("original", 0)
            if final_price is None:
                final_price = price_info.get("original", 0)
        else:
            final_price = price_info
            original_price = price_info

        period_display = format_period_description(days, language)

        # Create PriceInfo from already calculated prices
        # Note: original_price and final_price are calculated in the handler
        discount_percent = 0
        if original_price > final_price and original_price > 0:
            discount_percent = ((original_price - final_price) * 100) // original_price

        price_info_obj = PriceInfo(
            base_price=original_price,
            final_price=final_price,
            discount_percent=discount_percent
        )

        # Format button using unified system
        button_text = format_price_button(
            period_label=period_display,
            price_info=price_info_obj,
            format_price_func=texts.format_price,
            emphasize=False,
            add_exclamation=False
        )

        keyboard.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"extend_period_{days}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="menu_subscription")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_cryptobot_payment_keyboard(
    payment_id: str,
    local_payment_id: int,
    amount_usd: float,
    asset: str,
    bot_invoice_url: str,
    language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("PAY_WITH_COINS_BUTTON", "🪙 Оплатить"),
                url=bot_invoice_url
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("CHECK_STATUS_BUTTON", "📊 Проверить статус"),
                callback_data=f"check_cryptobot_{local_payment_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("MY_BALANCE_BUTTON", "💰 Мой баланс"),
                callback_data="menu_balance"
            )
        ]
    ])

def get_devices_management_keyboard(
    devices: List[dict], 
    pagination,
    language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    
    keyboard = []
    
    for i, device in enumerate(devices):
        platform = device.get('platform', 'Unknown')
        device_model = device.get('deviceModel', 'Unknown')
        device_info = f"{platform} - {device_model}"
        
        if len(device_info) > 25:
            device_info = device_info[:22] + "..."
        
        keyboard.append([
            InlineKeyboardButton(
                text=f"🔄 {device_info}",
                callback_data=f"reset_device_{i}_{pagination.page}"
            )
        ])
    
    if pagination.total_pages > 1:
        nav_row = []
        
        if pagination.has_prev:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("PAGINATION_PREV", "⬅️"),
                    callback_data=f"devices_page_{pagination.prev_page}"
                )
            )

        nav_row.append(
            InlineKeyboardButton(
                text=f"{pagination.page}/{pagination.total_pages}",
                callback_data="current_page"
            )
        )
        
        if pagination.has_next:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("PAGINATION_NEXT", "➡️"),
                    callback_data=f"devices_page_{pagination.next_page}"
                )
            )
        
        keyboard.append(nav_row)
    
    keyboard.append([
        InlineKeyboardButton(
            text=texts.t("RESET_ALL_DEVICES_BUTTON", "🔄 Сбросить все устройства"),
            callback_data="reset_all_devices"
        )
    ])
    
    keyboard.append([
        InlineKeyboardButton(
            text=texts.BACK,
            callback_data="subscription_settings"
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_updated_subscription_settings_keyboard(
    language: str = DEFAULT_LANGUAGE,
    show_countries_management: bool = True,
    tariff=None,  # Тариф подписки (если есть - ограничиваем настройки)
    subscription=None,  # Подписка (для проверки суточной паузы)
) -> InlineKeyboardMarkup:
    from app.config import settings

    texts = get_texts(language)
    keyboard = []

    # Если подписка на тарифе - отключаем страны, модем, трафик
    has_tariff = tariff is not None

    # Для суточных тарифов кнопка паузы теперь в главном меню подписки

    if show_countries_management and not has_tariff:
        keyboard.append([
            InlineKeyboardButton(text=texts.t("ADD_COUNTRIES_BUTTON", "🌐 Добавить страны"), callback_data="subscription_add_countries")
        ])

    if settings.is_traffic_selectable() and not has_tariff:
        keyboard.append([
            InlineKeyboardButton(text=texts.t("RESET_TRAFFIC_BUTTON", "🔄 Сбросить трафик"), callback_data="subscription_reset_traffic")
        ])
        keyboard.append([
            InlineKeyboardButton(text=texts.t("SWITCH_TRAFFIC_BUTTON", "🔄 Переключить трафик"), callback_data="subscription_switch_traffic")
        ])

    # Устройства: для тарифов - только если указана цена за устройство
    if has_tariff:
        tariff_device_price = getattr(tariff, 'device_price_kopeks', None)
        if tariff_device_price is not None and tariff_device_price > 0:
            keyboard.append([
                InlineKeyboardButton(
                    text=texts.t("CHANGE_DEVICES_BUTTON", "📱 Изменить устройства"),
                    callback_data="subscription_change_devices"
                )
            ])
    elif settings.is_devices_selection_enabled():
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("CHANGE_DEVICES_BUTTON", "📱 Изменить устройства"),
                callback_data="subscription_change_devices"
            )
        ])

    if settings.is_modem_enabled() and not has_tariff:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("MODEM_BUTTON", "📡 Модем"),
                callback_data="subscription_modem"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text=texts.t("MANAGE_DEVICES_BUTTON", "🔧 Управление устройствами"),
            callback_data="subscription_manage_devices"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="menu_subscription")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_device_reset_confirm_keyboard(device_info: str, device_index: int, page: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("RESET_DEVICE_CONFIRM_BUTTON", "✅ Да, сбросить это устройство"), 
                callback_data=f"confirm_reset_device_{device_index}_{page}"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.CANCEL, 
                callback_data=f"devices_page_{page}"
            )
        ]
    ])


def get_device_management_help_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("DEVICE_CONNECTION_HELP", "❓ Как подключить устройство заново?"),
                callback_data="device_connection_help"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("MANAGE_DEVICES_BUTTON", "🔧 Управление устройствами"),
                callback_data="subscription_manage_devices"
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t("BACK_TO_SUBSCRIPTION", "⬅️ К подписке"),
                callback_data="menu_subscription"
            )
        ]
    ])


# ==================== TICKET KEYBOARDS ====================

def get_ticket_cancel_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("CANCEL_TICKET_CREATION", "❌ Отменить создание тикета"),
                callback_data="cancel_ticket_creation"
            )
        ]
    ])


def get_my_tickets_keyboard(
    tickets: List[dict],
    current_page: int = 1,
    total_pages: int = 1,
    language: str = DEFAULT_LANGUAGE,
    page_prefix: str = "my_tickets_page_"
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    for ticket in tickets:
        status_emoji = ticket.get('status_emoji', '❓')
        # Override status emoji for closed tickets in admin list
        if ticket.get('is_closed', False):
            status_emoji = '✅'
        title = ticket.get('title', 'Без названия')[:25]
        button_text = f"{status_emoji} #{ticket['id']} {title}"
        
        keyboard.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"view_ticket_{ticket['id']}"
            )
        ])
    
    # Пагинация
    if total_pages > 1:
        nav_row = []
        
        if current_page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("PAGINATION_PREV", "⬅️"),
                    callback_data=f"{page_prefix}{current_page - 1}"
                )
            )
        
        nav_row.append(
            InlineKeyboardButton(
                text=f"{current_page}/{total_pages}",
                callback_data="current_page"
            )
        )
        
        if current_page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("PAGINATION_NEXT", "➡️"),
                    callback_data=f"{page_prefix}{current_page + 1}"
                )
            )
        
        keyboard.append(nav_row)
    
    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="menu_support")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_ticket_view_keyboard(
    ticket_id: int,
    is_closed: bool = False,
    language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    if not is_closed:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("REPLY_TO_TICKET", "💬 Ответить"),
                callback_data=f"reply_ticket_{ticket_id}"
            )
        ])
    
    if not is_closed:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("CLOSE_TICKET", "🔒 Закрыть тикет"),
                callback_data=f"close_ticket_{ticket_id}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="my_tickets")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_ticket_reply_cancel_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("CANCEL_REPLY", "❌ Отменить ответ"),
                callback_data="cancel_ticket_reply"
            )
        ]
    ])


# ==================== ADMIN TICKET KEYBOARDS ====================

def get_admin_tickets_keyboard(
    tickets: List[dict],
    current_page: int = 1,
    total_pages: int = 1,
    language: str = DEFAULT_LANGUAGE,
    scope: str = "all",
    *,
    back_callback: str = "admin_submenu_support"
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    # Разделяем открытые/закрытые
    open_rows = []
    closed_rows = []
    for ticket in tickets:
        status_emoji = ticket.get('status_emoji', '❓')
        if ticket.get('is_closed', False):
            status_emoji = '✅'
        user_name = ticket.get('user_name', 'Unknown')
        username = ticket.get('username')
        telegram_id = ticket.get('telegram_id')
        # Сформируем компактное отображение: Имя (@username | ID)
        name_parts = [user_name[:15]]
        contact_parts = []
        if username:
            contact_parts.append(f"@{username}")
        if telegram_id:
            contact_parts.append(str(telegram_id))
        if contact_parts:
            name_parts.append(f"({' | '.join(contact_parts)})")
        name_display = ' '.join(name_parts)
        title = ticket.get('title', 'Без названия')[:20]
        locked_emoji = ticket.get('locked_emoji', '')
        button_text = f"{status_emoji} #{ticket['id']} {locked_emoji} {name_display}: {title}".replace("  ", " ")
        row = [InlineKeyboardButton(text=button_text, callback_data=f"admin_view_ticket_{ticket['id']}")]
        if ticket.get('is_closed', False):
            closed_rows.append(row)
        else:
            open_rows.append(row)

    # Scope switcher
    switch_row = []
    switch_row.append(InlineKeyboardButton(text=texts.t("OPEN_TICKETS", "🔴 Открытые"), callback_data="admin_tickets_scope_open"))
    switch_row.append(InlineKeyboardButton(text=texts.t("CLOSED_TICKETS", "🟢 Закрытые"), callback_data="admin_tickets_scope_closed"))
    keyboard.append(switch_row)

    if open_rows and scope in ("all", "open"):
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("ADMIN_CLOSE_ALL_OPEN_TICKETS", "🔒 Закрыть все открытые"),
                callback_data="admin_tickets_close_all_open"
            )
        ])
        keyboard.append([InlineKeyboardButton(text=texts.t("OPEN_TICKETS_HEADER", "Открытые тикеты"), callback_data="noop")])
        keyboard.extend(open_rows)
    if closed_rows and scope in ("all", "closed"):
        keyboard.append([InlineKeyboardButton(text=texts.t("CLOSED_TICKETS_HEADER", "Закрытые тикеты"), callback_data="noop")])
        keyboard.extend(closed_rows)
    
    # Пагинация
    if total_pages > 1:
        nav_row = []
        
        if current_page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("PAGINATION_PREV", "⬅️"),
                    callback_data=f"admin_tickets_page_{scope}_{current_page - 1}"
                )
            )
        
        nav_row.append(
            InlineKeyboardButton(
                text=f"{current_page}/{total_pages}",
                callback_data="current_page"
            )
        )
        
        if current_page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t("PAGINATION_NEXT", "➡️"),
                    callback_data=f"admin_tickets_page_{scope}_{current_page + 1}"
                )
            )
        
        keyboard.append(nav_row)
    
    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_admin_ticket_view_keyboard(
    ticket_id: int,
    is_closed: bool = False,
    language: str = DEFAULT_LANGUAGE,
    *,
    is_user_blocked: bool = False
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    
    if not is_closed:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("REPLY_TO_TICKET", "💬 Ответить"),
                callback_data=f"admin_reply_ticket_{ticket_id}"
            )
        ])
    
    if not is_closed:
        keyboard.append([
            InlineKeyboardButton(
                text=texts.t("CLOSE_TICKET", "🔒 Закрыть тикет"),
                callback_data=f"admin_close_ticket_{ticket_id}"
            )
        ])
    
    # Блок-контролы: когда не заблокирован — показать два варианта, когда заблокирован — только "Разблокировать"
    if is_user_blocked:
        keyboard.append([
            InlineKeyboardButton(text=texts.t("UNBLOCK", "✅ Разблокировать"), callback_data=f"admin_unblock_user_ticket_{ticket_id}")
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(text=texts.t("BLOCK_FOREVER", "🚫 Заблокировать"), callback_data=f"admin_block_user_perm_ticket_{ticket_id}"),
            InlineKeyboardButton(text=texts.t("BLOCK_BY_TIME", "⏳ Блок по времени"), callback_data=f"admin_block_user_ticket_{ticket_id}")
        ])
    
    keyboard.append([
        InlineKeyboardButton(text=texts.BACK, callback_data="admin_tickets")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_admin_ticket_reply_cancel_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=texts.t("CANCEL_REPLY", "❌ Отменить ответ"),
                callback_data="cancel_admin_ticket_reply"
            )
        ]
    ])
