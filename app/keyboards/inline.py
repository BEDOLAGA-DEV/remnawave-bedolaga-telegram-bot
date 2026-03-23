from datetime import UTC, datetime

import structlog
from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings
from app.database.models import User
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.price_display import PriceInfo, format_price_button
from app.utils.pricing_utils import (
    apply_percentage_discount,
    format_period_description,
)
from app.utils.subscription_utils import (
    get_display_subscription_link,
    get_happ_cryptolink_redirect_link,
)


logger = structlog.get_logger(__name__)


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
    custom_buttons: list[InlineKeyboardButton] | None = None,
    user=None,  # Добавляем параметр пользователя для получения данных
) -> InlineKeyboardMarkup:
    """
    Асинхронная версия get_main_menu_keyboard с поддержкой конструктора меню.

    Если MENU_LAYOUT_ENABLED=True, использует конфигурацию из БД.
    Иначе делегирует в синхронную версию.
    """
    if settings.MENU_LAYOUT_ENABLED:
        from app.services.menu_layout_service import MenuContext, MenuLayoutService

        # Получаем данные для плейсхолдеров
        subscription_days_left = 0
        traffic_used_gb = 0.0
        traffic_left_gb = 0.0
        referral_count = 0
        referral_earnings_kopeks = 0
        registration_days = 0
        promo_group_id = None
        has_autopay = False
        username = ''

        # Заполняем данными из подписки
        if subscription:
            # Дни до окончания подписки
            if hasattr(subscription, 'days_left'):
                # Используем свойство из модели, которое правильно вычисляет дни в UTC
                subscription_days_left = subscription.days_left
            elif hasattr(subscription, 'end_date') and subscription.end_date:
                # Fallback: вычисляем вручную, используя UTC
                now_utc = datetime.now(UTC)
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
                now_utc = datetime.now(UTC)
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
            logger.error('Error getting referral data', error=e)

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


_LANGUAGE_DISPLAY_NAMES = {
    'ru': '🇷🇺 Русский',
    'ru-ru': '🇷🇺 Русский',
    'en': '🇬🇧 English',
    'en-us': '🇺🇸 English',
    'en-gb': '🇬🇧 English',
    'ua': '🇺🇦 Українська',
    'uk': '🇺🇦 Українська',
    'uk-ua': '🇺🇦 Українська',
    'kk': '🇰🇿 Қазақша',
    'kk-kz': '🇰🇿 Қазақша',
    'kz': '🇰🇿 Қазақша',
    'uz': '🇺🇿 Oʻzbekcha',
    'uz-uz': '🇺🇿 Oʻzbekcha',
    'tr': '🇹🇷 Türkçe',
    'tr-tr': '🇹🇷 Türkçe',
    'pl': '🇵🇱 Polski',
    'pl-pl': '🇵🇱 Polski',
    'de': '🇩🇪 Deutsch',
    'de-de': '🇩🇪 Deutsch',
    'fr': '🇫🇷 Français',
    'fr-fr': '🇫🇷 Français',
    'es': '🇪🇸 Español',
    'es-es': '🇪🇸 Español',
    'it': '🇮🇹 Italiano',
    'it-it': '🇮🇹 Italiano',
    'pt': '🇵🇹 Português',
    'pt-pt': '🇵🇹 Português',
    'pt-br': '🇧🇷 Português',
    'zh': '🇨🇳 中文',
    'zh-cn': '🇨🇳 中文 (简体)',
    'zh-hans': '🇨🇳 中文 (简体)',
    'zh-tw': '🇹🇼 中文 (繁體)',
    'zh-hant': '🇹🇼 中文 (繁體)',
    'vi': '🇻🇳 Tiếng Việt',
    'vi-vn': '🇻🇳 Tiếng Việt',
    'fa': '🇮🇷 فارسی',
    'fa-ir': '🇮🇷 فارسی',
}


def get_rules_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.RULES_ACCEPT, callback_data='rules_accept'),
                InlineKeyboardButton(text=texts.RULES_DECLINE, callback_data='rules_decline'),
            ]
        ]
    )


def get_privacy_policy_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.PRIVACY_POLICY_ACCEPT, callback_data='privacy_policy_accept'),
                InlineKeyboardButton(text=texts.PRIVACY_POLICY_DECLINE, callback_data='privacy_policy_decline'),
            ]
        ]
    )


def get_channel_sub_keyboard(
    channels: list[dict] | str | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    """Subscription keyboard for required channels.

    Supports Bot API 9.4 colored buttons via ``style`` parameter:
    - subscribed channels → green (``style='success'``)
    - unsubscribed channels → blue (``style='primary'``)

    Args:
        channels: List of dicts with 'channel_link', 'title', and optional
                  'is_subscribed' keys, OR a string (legacy single channel_link).
        language: Locale code for button text.
    """
    texts = get_texts(language)
    buttons: list[list[InlineKeyboardButton]] = []

    if isinstance(channels, str):
        # Legacy: single channel link string
        if channels:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('CHANNEL_SUBSCRIBE_BUTTON', '🔗 Подписаться'),
                        url=channels,
                        style='primary',
                    )
                ]
            )
    elif isinstance(channels, list):
        for ch in channels:
            link = ch.get('channel_link')
            title = ch.get('title')
            is_subscribed = ch.get('is_subscribed', False)
            if link:
                if is_subscribed:
                    label = f'✅ {title}' if title else '✅'
                    buttons.append([InlineKeyboardButton(text=label, url=link, style='success')])
                else:
                    label = title or texts.t('CHANNEL_SUBSCRIBE_BUTTON', '🔗 Подписаться')
                    buttons.append([InlineKeyboardButton(text=label, url=link, style='primary')])

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.t('CHANNEL_CHECK_BUTTON', '✅ Я подписался'),
                callback_data='sub_channel_check',
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_post_registration_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('POST_REGISTRATION_TRIAL_BUTTON', '🚀 Подключиться бесплатно 🚀'),
                    callback_data='trial_activate',
                )
            ],
            [InlineKeyboardButton(text=texts.t('SKIP_BUTTON', 'Пропустить ➡️'), callback_data='back_to_menu')],
        ]
    )


def get_language_selection_keyboard(
    current_language: str | None = None,
    *,
    include_back: bool = False,
    language: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    available_languages = settings.get_available_languages()

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    normalized_current = (current_language or '').lower()

    for index, lang_code in enumerate(available_languages, start=1):
        normalized_code = lang_code.lower()
        display_name = _LANGUAGE_DISPLAY_NAMES.get(
            normalized_code,
            normalized_code.upper(),
        )

        prefix = '✅ ' if normalized_code == normalized_current and normalized_current else ''

        row.append(
            InlineKeyboardButton(
                text=f'{prefix}{display_name}',
                callback_data=f'language_select:{normalized_code}',
            )
        )

        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    if include_back:
        texts = get_texts(language)
        buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _get_balance_text(cached_styles: dict, language: str, texts, balance_kopeks: int) -> str:
    """Build balance button text with formatting."""
    bal_cfg = cached_styles.get('balance', {})
    safe_balance = balance_kopeks or 0

    # Custom label overrides the whole text including balance amount
    custom_bal = bal_cfg.get('labels', {}).get(language, '')
    if custom_bal:
        return custom_bal
    if hasattr(texts, 'BALANCE_BUTTON') and safe_balance > 0:
        return texts.BALANCE_BUTTON.format(balance=texts.format_price(safe_balance))
    return texts.t('BALANCE_BUTTON_DEFAULT', '💰 Баланс: {balance}').format(
        balance=texts.format_price(safe_balance),
    )


def _is_support_enabled() -> bool:
    """Check if support menu is enabled."""
    try:
        from app.services.support_settings_service import SupportSettingsService

        return SupportSettingsService.is_support_menu_enabled()
    except Exception:
        return settings.SUPPORT_MENU_ENABLED


def _build_cabinet_main_menu_keyboard(
    language: str,
    texts,
    *,
    is_admin: bool,
    is_moderator: bool,
    balance_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    """Build the main-menu keyboard for Cabinet mode.

    Row layout and button arrangement are driven by the cached menu layout
    (``get_cached_menu_layout``).  Each row specifies which buttons it contains
    and how many fit per keyboard row (``max_per_row``).
    """
    from app.utils.button_styles_cache import CALLBACK_TO_SECTION, get_cached_button_styles
    from app.utils.menu_layout_cache import get_cached_menu_layout
    from app.utils.miniapp_buttons import (
        CALLBACK_TO_CABINET_STYLE,
        _resolve_style,
        build_cabinet_url,
    )

    global_style = _resolve_style((settings.CABINET_BUTTON_STYLE or '').strip())
    cached_styles = get_cached_button_styles()
    layout = get_cached_menu_layout()
    custom_buttons_cfg: dict[str, dict] = layout.get('custom_buttons', {})

    def _cabinet_button(
        text: str,
        path: str,
        callback_fallback: str,
        *,
        style: str | None = None,
        icon_custom_emoji_id: str | None = None,
    ) -> InlineKeyboardButton:
        url = build_cabinet_url(path)
        if url:
            section = CALLBACK_TO_SECTION.get(callback_fallback)
            section_cfg = cached_styles.get(section or '', {}) if section else {}

            # 'default' in per-section config means "no color" — do not fall through.
            if style:
                resolved = _resolve_style(style)
            elif section_cfg.get('style'):
                resolved = _resolve_style(section_cfg['style'])
            else:
                resolved = global_style or _resolve_style(CALLBACK_TO_CABINET_STYLE.get(callback_fallback))
            resolved_emoji = icon_custom_emoji_id or section_cfg.get('icon_custom_emoji_id') or None

            return InlineKeyboardButton(
                text=text,
                web_app=types.WebAppInfo(url=url),
                style=resolved,
                icon_custom_emoji_id=resolved_emoji or None,
            )
        return InlineKeyboardButton(text=text, callback_data=callback_fallback)

    # -- Collect row definitions sorted by row_N key --
    row_keys = sorted(
        (k for k in layout if k.startswith('row_')),
        key=lambda k: int(k.split('_', 1)[1]) if k.split('_', 1)[1].isdigit() else 0,
    )

    keyboard_rows: list[list[InlineKeyboardButton]] = []

    for row_key in row_keys:
        row_def = layout[row_key]
        btn_ids: list[str] = row_def.get('buttons', [])
        max_per_row: int = row_def.get('max_per_row', 1)
        row_buttons: list[InlineKeyboardButton] = []

        for btn_id in btn_ids:
            # --- Custom URL buttons ---
            if btn_id.startswith('custom_'):
                custom_cfg = custom_buttons_cfg.get(btn_id)
                if not custom_cfg or not custom_cfg.get('url') or not custom_cfg.get('enabled', True):
                    continue
                custom_text = (
                    custom_cfg.get('labels', {}).get(language, '')
                    or custom_cfg.get('labels', {}).get('ru', '')
                    or 'Link'
                )
                resolved_style = _resolve_style(custom_cfg.get('style'))
                resolved_emoji = custom_cfg.get('icon_custom_emoji_id') or None
                open_in = custom_cfg.get('open_in', 'external')
                link_kwarg = (
                    {'web_app': types.WebAppInfo(url=custom_cfg['url'])}
                    if open_in == 'webapp'
                    else {'url': custom_cfg['url']}
                )
                row_buttons.append(
                    InlineKeyboardButton(
                        text=custom_text,
                        **link_kwarg,
                        style=resolved_style,
                        icon_custom_emoji_id=resolved_emoji,
                    ),
                )
                continue

            # --- Built-in buttons ---
            section_cfg = cached_styles.get(btn_id, {})

            match btn_id:
                case 'home':
                    if not section_cfg.get('enabled', True):
                        continue
                    home_text = section_cfg.get('labels', {}).get(language, '') or texts.t(
                        'MENU_PROFILE', '👤 Личный кабинет'
                    )
                    row_buttons.append(_cabinet_button(home_text, '/', 'menu_profile_unavailable'))

                case 'subscription':
                    if not section_cfg.get('enabled', True):
                        continue
                    default_sub_text = (
                        texts.t('MY_SUBSCRIPTIONS_BUTTON', '📱 Мои подписки')
                        if settings.is_multi_tariff_enabled()
                        else texts.MENU_SUBSCRIPTION
                    )
                    sub_text = section_cfg.get('labels', {}).get(language, '') or default_sub_text
                    row_buttons.append(_cabinet_button(sub_text, '/subscription', 'menu_subscription'))

                case 'balance':
                    if not section_cfg.get('enabled', True):
                        continue
                    balance_text = _get_balance_text(cached_styles, language, texts, balance_kopeks)
                    row_buttons.append(_cabinet_button(balance_text, '/balance', 'menu_balance'))

                case 'referral':
                    if not settings.is_referral_program_enabled():
                        continue
                    if not section_cfg.get('enabled', True):
                        continue
                    ref_text = section_cfg.get('labels', {}).get(language, '') or texts.MENU_REFERRALS
                    row_buttons.append(_cabinet_button(ref_text, '/referral', 'menu_referrals'))

                case 'support':
                    if not _is_support_enabled():
                        continue
                    if not section_cfg.get('enabled', True):
                        continue
                    sup_text = section_cfg.get('labels', {}).get(language, '') or texts.MENU_SUPPORT
                    row_buttons.append(_cabinet_button(sup_text, '/support', 'menu_support'))

                case 'info':
                    if not section_cfg.get('enabled', True):
                        continue
                    info_text = section_cfg.get('labels', {}).get(language, '') or texts.t('MENU_INFO', 'ℹ️ Инфо')
                    row_buttons.append(_cabinet_button(info_text, '/info', 'menu_info'))

                case 'language':
                    if not section_cfg.get('enabled', True):
                        continue
                    if not settings.is_language_selection_enabled():
                        continue
                    lang_text = section_cfg.get('labels', {}).get(language, '') or texts.MENU_LANGUAGE
                    resolved_lang_emoji = section_cfg.get('icon_custom_emoji_id') or None
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=lang_text,
                            callback_data='menu_language',
                            icon_custom_emoji_id=resolved_lang_emoji,
                        )
                    )

                case 'admin':
                    if not is_admin:
                        continue
                    admin_row = [InlineKeyboardButton(text=texts.MENU_ADMIN, callback_data='admin_panel')]
                    if section_cfg.get('enabled', True):
                        admin_web_text = section_cfg.get('labels', {}).get(language, '') or '🖥 Веб-Админка'
                        admin_row.append(_cabinet_button(admin_web_text, '/admin', 'admin_panel'))
                    keyboard_rows.append(admin_row)
                    continue  # bypass max_per_row chunking

        # Split collected buttons into keyboard rows respecting max_per_row
        if row_buttons:
            for i in range(0, len(row_buttons), max_per_row):
                keyboard_rows.append(row_buttons[i : i + max_per_row])

    # -- Moderator panel (only when not admin — admin row handled above) --
    if is_moderator and not is_admin:
        keyboard_rows.append([InlineKeyboardButton(text='🧑‍⚖️ Модерация', callback_data='moderator_panel')])

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
    custom_buttons: list[InlineKeyboardButton] | None = None,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    if settings.is_cabinet_mode():
        return _build_cabinet_main_menu_keyboard(
            language,
            texts,
            is_admin=is_admin,
            is_moderator=is_moderator,
            balance_kopeks=balance_kopeks,
        )

    if settings.DEBUG:
        logger.debug(
            'DEBUG KEYBOARD',
            language=language,
            is_admin=is_admin,
            has_had_paid=has_had_paid_subscription,
            has_active=has_active_subscription,
            sub_active=subscription_is_active,
            balance=balance_kopeks,
        )

    safe_balance = balance_kopeks or 0
    if hasattr(texts, 'BALANCE_BUTTON') and safe_balance > 0:
        balance_button_text = texts.BALANCE_BUTTON.format(balance=texts.format_price(safe_balance))
    else:
        balance_button_text = texts.t(
            'BALANCE_BUTTON_DEFAULT',
            '💰 Баланс: {balance}',
        ).format(balance=texts.format_price(safe_balance))

    keyboard: list[list[InlineKeyboardButton]] = []
    paired_buttons: list[InlineKeyboardButton] = []

    if has_active_subscription and subscription_is_active:
        connect_mode = settings.CONNECT_BUTTON_MODE
        subscription_link = get_display_subscription_link(subscription)

        def _fallback_connect_button() -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                callback_data='subscription_connect',
            )

        if connect_mode == 'miniapp_subscription':
            if subscription_link:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            web_app=types.WebAppInfo(url=subscription_link),
                        )
                    ]
                )
            else:
                keyboard.append([_fallback_connect_button()])
        elif connect_mode == 'miniapp_custom':
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                    )
                ]
            )
        elif connect_mode == 'link':
            if subscription_link:
                keyboard.append(
                    [InlineKeyboardButton(text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), url=subscription_link)]
                )
            else:
                keyboard.append([_fallback_connect_button()])
        elif connect_mode == 'happ_cryptolink':
            if subscription_link:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            callback_data=(
                                'subscription_connect'
                                if settings.is_multi_tariff_enabled()
                                else 'open_subscription_link'
                            ),
                        )
                    ]
                )
            else:
                keyboard.append([_fallback_connect_button()])
        else:
            keyboard.append([_fallback_connect_button()])

        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            keyboard.append(happ_row)
        sub_btn_text = (
            texts.t('MY_SUBSCRIPTIONS_BUTTON', '📱 Мои подписки')
            if settings.is_multi_tariff_enabled()
            else texts.MENU_SUBSCRIPTION
        )
        paired_buttons.append(InlineKeyboardButton(text=sub_btn_text, callback_data='menu_subscription'))

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
                InlineKeyboardButton(
                    text=texts.t('BUY_TRAFFIC_BUTTON', '📈 Докупить трафик'), callback_data='buy_traffic'
                )
            )

    keyboard.append([InlineKeyboardButton(text=balance_button_text, callback_data='menu_balance')])

    show_trial = not has_had_paid_subscription and not has_active_subscription

    show_buy = not has_active_subscription or not subscription_is_active
    current_subscription = subscription
    bool(
        current_subscription
        and not getattr(current_subscription, 'is_trial', False)
        and getattr(current_subscription, 'is_active', False)
    )
    simple_purchase_button = None
    if settings.SIMPLE_SUBSCRIPTION_ENABLED:
        simple_purchase_button = InlineKeyboardButton(
            text=texts.MENU_SIMPLE_SUBSCRIPTION,
            callback_data='simple_subscription_purchase',
        )

    subscription_buttons: list[InlineKeyboardButton] = []

    if show_trial:
        subscription_buttons.append(InlineKeyboardButton(text=texts.MENU_TRIAL, callback_data='menu_trial'))

    if show_buy:
        subscription_buttons.append(InlineKeyboardButton(text=texts.MENU_BUY_SUBSCRIPTION, callback_data='menu_buy'))

    if subscription_buttons:
        paired_buttons.extend(subscription_buttons)
    if simple_purchase_button:
        paired_buttons.append(simple_purchase_button)

    if show_resume_checkout or has_saved_cart:
        resume_callback = 'return_to_saved_cart' if has_saved_cart else 'subscription_resume_checkout'
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
    paired_buttons.append(InlineKeyboardButton(text=texts.MENU_PROMOCODE, callback_data='menu_promocode'))

    # Добавляем кнопку рефералов, только если программа включена
    if settings.is_referral_program_enabled():
        paired_buttons.append(InlineKeyboardButton(text=texts.MENU_REFERRALS, callback_data='menu_referrals'))

    # Добавляем кнопку конкурсов
    if settings.CONTESTS_ENABLED and settings.CONTESTS_BUTTON_VISIBLE:
        paired_buttons.append(
            InlineKeyboardButton(text=texts.t('CONTESTS_BUTTON', '🎲 Конкурсы'), callback_data='contests_menu')
        )

    try:
        from app.services.support_settings_service import SupportSettingsService

        support_enabled = SupportSettingsService.is_support_menu_enabled()
    except Exception:
        support_enabled = settings.SUPPORT_MENU_ENABLED

    if support_enabled:
        paired_buttons.append(InlineKeyboardButton(text=texts.MENU_SUPPORT, callback_data='menu_support'))

    # Добавляем кнопку активации
    if settings.ACTIVATE_BUTTON_VISIBLE:
        paired_buttons.append(InlineKeyboardButton(text=settings.ACTIVATE_BUTTON_TEXT, callback_data='activate_button'))

    paired_buttons.append(
        InlineKeyboardButton(
            text=texts.t('MENU_INFO', 'ℹ️ Инфо'),
            callback_data='menu_info',
        )
    )

    if settings.is_language_selection_enabled():
        paired_buttons.append(InlineKeyboardButton(text=texts.MENU_LANGUAGE, callback_data='menu_language'))

    for i in range(0, len(paired_buttons), 2):
        row = paired_buttons[i : i + 2]
        keyboard.append(row)

    if settings.DEBUG:
        logger.debug('DEBUG KEYBOARD: админ кнопка', is_admin=is_admin)

    if is_admin:
        if settings.DEBUG:
            logger.debug('DEBUG KEYBOARD: Админ кнопка ДОБАВЛЕНА')
        keyboard.append([InlineKeyboardButton(text=texts.MENU_ADMIN, callback_data='admin_panel')])
    elif settings.DEBUG:
        logger.debug('DEBUG KEYBOARD: Админ кнопка НЕ добавлена')
    # Moderator access (limited support panel)
    if (not is_admin) and is_moderator:
        keyboard.append([InlineKeyboardButton(text='🧑‍⚖️ Модерация', callback_data='moderator_panel')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_info_menu_keyboard(
    language: str = DEFAULT_LANGUAGE,
    show_privacy_policy: bool = False,
    show_public_offer: bool = False,
    show_faq: bool = False,
    show_promo_groups: bool = False,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    buttons: list[list[InlineKeyboardButton]] = []

    if show_faq:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('MENU_FAQ', '❓ FAQ'),
                    callback_data='menu_faq',
                )
            ]
        )

    if show_promo_groups:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('MENU_PROMO_GROUPS_INFO', '🎯 Промогруппы'),
                    callback_data='menu_info_promo_groups',
                )
            ]
        )

    if show_privacy_policy:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('MENU_PRIVACY_POLICY', '🛡️ Политика конф.'),
                    callback_data='menu_privacy_policy',
                )
            ]
        )

    if show_public_offer:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('MENU_PUBLIC_OFFER', '📄 Оферта'),
                    callback_data='menu_public_offer',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.MENU_RULES, callback_data='menu_rules')])

    server_status_mode = settings.get_server_status_mode()
    server_status_text = texts.t('MENU_SERVER_STATUS', '📊 Статус серверов')

    if server_status_mode == 'external_link':
        status_url = settings.get_server_status_external_url()
        if status_url:
            buttons.append([InlineKeyboardButton(text=server_status_text, url=status_url)])
    elif server_status_mode == 'external_link_miniapp':
        status_url = settings.get_server_status_external_url()
        if status_url:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=server_status_text,
                        web_app=types.WebAppInfo(url=status_url),
                    )
                ]
            )
    elif server_status_mode == 'xray':
        buttons.append(
            [
                InlineKeyboardButton(
                    text=server_status_text,
                    callback_data='menu_server_status',
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_happ_download_button_row(texts) -> list[InlineKeyboardButton] | None:
    if not settings.is_happ_download_button_enabled():
        return None

    return [
        InlineKeyboardButton(
            text=texts.t('HAPP_DOWNLOAD_BUTTON', '⬇️ Скачать Happ'), callback_data='subscription_happ_download'
        )
    ]


def get_happ_cryptolink_keyboard(
    subscription_link: str,
    language: str = DEFAULT_LANGUAGE,
    redirect_link: str | None = None,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    final_redirect_link = redirect_link or get_happ_cryptolink_redirect_link(subscription_link)

    buttons: list[list[InlineKeyboardButton]] = []

    if final_redirect_link:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                    url=final_redirect_link,
                )
            ]
        )

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    text=texts.t('HAPP_PLATFORM_IOS', '🍎 iOS'),
                    callback_data='happ_download_ios',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('HAPP_PLATFORM_ANDROID', '🤖 Android'),
                    callback_data='happ_download_android',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('HAPP_PLATFORM_MACOS', '🖥️ Mac OS'),
                    callback_data='happ_download_macos',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('HAPP_PLATFORM_WINDOWS', '💻 Windows'),
                    callback_data='happ_download_windows',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                    callback_data='back_to_menu',
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_happ_download_platform_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t('HAPP_PLATFORM_IOS', '🍎 iOS'), callback_data='happ_download_ios')],
        [
            InlineKeyboardButton(
                text=texts.t('HAPP_PLATFORM_ANDROID', '🤖 Android'), callback_data='happ_download_android'
            )
        ],
        [InlineKeyboardButton(text=texts.t('HAPP_PLATFORM_MACOS', '🖥️ Mac OS'), callback_data='happ_download_macos')],
        [
            InlineKeyboardButton(
                text=texts.t('HAPP_PLATFORM_WINDOWS', '💻 Windows'), callback_data='happ_download_windows'
            )
        ],
        [InlineKeyboardButton(text=texts.BACK, callback_data='happ_download_close')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_happ_download_link_keyboard(language: str, link: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = [
        [InlineKeyboardButton(text=texts.t('HAPP_DOWNLOAD_OPEN_LINK', '🔗 Открыть ссылку'), url=link)],
        [InlineKeyboardButton(text=texts.BACK, callback_data='happ_download_back')],
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_back_keyboard(language: str = DEFAULT_LANGUAGE, callback_data: str = 'back_to_menu') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=texts.BACK, callback_data=callback_data)]])


def get_server_status_keyboard(
    language: str,
    current_page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=texts.t('SERVER_STATUS_REFRESH', '🔄 Обновить'),
                callback_data=f'server_status_page:{current_page}',
            )
        ]
    ]

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []

        if current_page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('SERVER_STATUS_PREV_PAGE', '⬅️ Назад'),
                    callback_data=f'server_status_page:{current_page - 1}',
                )
            )

        if current_page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('SERVER_STATUS_NEXT_PAGE', 'Вперед ➡️'),
                    callback_data=f'server_status_page:{current_page + 1}',
                )
            )

        if nav_row:
            keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

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
            and last_row[0].callback_data in {'menu_balance', 'back_to_menu'}
        ):
            keyboard.inline_keyboard[-1][0] = InlineKeyboardButton(
                text=texts.t('PAYMENT_RETURN_HOME_BUTTON', '🏠 На главную'),
                callback_data='back_to_menu',
            )
            back_row_index = len(keyboard.inline_keyboard) - 1

    # Если есть сохраненная корзина, добавляем кнопку возврата к оформлению
    if has_saved_cart:
        return_row = [
            InlineKeyboardButton(
                text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT,
                callback_data='return_to_saved_cart',
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
    language: str = DEFAULT_LANGUAGE, has_subscription: bool = False, is_trial: bool = False, subscription=None
) -> InlineKeyboardMarkup:
    from app.config import settings

    texts = get_texts(language)
    keyboard = []

    # Sub ID suffix for multi-tariff callback routing
    _sub_suffix = (
        f':{subscription.id}'
        if settings.is_multi_tariff_enabled() and subscription and hasattr(subscription, 'id')
        else ''
    )

    if has_subscription:
        subscription_link = get_display_subscription_link(subscription) if subscription else None
        if subscription_link:
            connect_mode = settings.CONNECT_BUTTON_MODE

            if connect_mode == 'miniapp_subscription':
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            web_app=types.WebAppInfo(url=subscription_link),
                        )
                    ]
                )
            elif connect_mode == 'miniapp_custom':
                if settings.MINIAPP_CUSTOM_URL:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                            )
                        ]
                    )
                else:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                callback_data=f'subscription_connect{_sub_suffix}',
                            )
                        ]
                    )
            elif connect_mode == 'link':
                keyboard.append(
                    [InlineKeyboardButton(text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), url=subscription_link)]
                )
            elif connect_mode == 'happ_cryptolink':
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            callback_data=f'open_subscription_link{_sub_suffix}',
                        )
                    ]
                )
            else:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                            callback_data=f'subscription_connect{_sub_suffix}',
                        )
                    ]
                )
        elif settings.CONNECT_BUTTON_MODE == 'miniapp_custom':
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        web_app=types.WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                    )
                ]
            )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                        callback_data=f'subscription_connect{_sub_suffix}',
                    )
                ]
            )

        happ_row = get_happ_download_button_row(texts)
        if happ_row:
            keyboard.append(happ_row)

        if is_trial:
            keyboard.append(
                [InlineKeyboardButton(text=texts.MENU_BUY_SUBSCRIPTION, callback_data='subscription_upgrade')]
            )
        else:
            # Проверяем, является ли тариф суточным
            tariff = getattr(subscription, 'tariff', None) if subscription else None
            is_daily_tariff = tariff and getattr(tariff, 'is_daily', False)

            if is_daily_tariff:
                # Для суточного тарифа: проверяем статус подписки
                from app.database.models import SubscriptionStatus

                sub_status = getattr(subscription, 'status', None)
                is_paused = getattr(subscription, 'is_daily_paused', False)
                is_inactive = sub_status in (
                    SubscriptionStatus.DISABLED.value,
                    SubscriptionStatus.EXPIRED.value,
                    SubscriptionStatus.LIMITED.value,
                )

                if is_inactive or is_paused:
                    # Подписка остановлена (системой или пользователем) — показываем «Возобновить»
                    pause_text = texts.t('RESUME_DAILY_BUTTON', '▶️ Возобновить подписку')
                else:
                    pause_text = texts.t('PAUSE_DAILY_BUTTON', '⏸️ Приостановить подписку')
                keyboard.append(
                    [InlineKeyboardButton(text=pause_text, callback_data='toggle_daily_subscription_pause')]
                )
            else:
                # Для обычного тарифа: [Продлить] [Автоплатеж]
                keyboard.append(
                    [
                        InlineKeyboardButton(text=texts.MENU_EXTEND_SUBSCRIPTION, callback_data='subscription_extend'),
                        InlineKeyboardButton(
                            text=texts.t('AUTOPAY_BUTTON', '💳 Автоплатеж'),
                            callback_data='subscription_autopay',
                        ),
                    ]
                )

            # Ряд: [Настройки] [Тариф] (если режим тарифов)
            settings_row = [
                InlineKeyboardButton(
                    text=texts.t('SUBSCRIPTION_SETTINGS_BUTTON', '⚙️ Настройки'),
                    callback_data='subscription_settings',
                )
            ]
            if settings.is_tariffs_mode() and subscription:
                # Для суточных тарифов переходим на список тарифов, для обычных - мгновенное переключение
                tariff_callback = 'tariff_switch' if is_daily_tariff else 'instant_switch'
                settings_row.append(
                    InlineKeyboardButton(
                        text=texts.t('CHANGE_TARIFF_BUTTON', '📦 Тариф'), callback_data=tariff_callback
                    )
                )
            keyboard.append(settings_row)

            # Кнопка докупки трафика для платных подписок
            # В режиме тарифов проверяем can_topup_traffic() у тарифа, в классическом - глобальные настройки
            show_traffic_topup = False
            if subscription and (subscription.traffic_limit_gb or 0) > 0:
                if settings.is_tariffs_mode() and tariff:
                    show_traffic_topup = tariff.can_topup_traffic()
                elif settings.is_traffic_topup_enabled() and not settings.is_traffic_topup_blocked():
                    show_traffic_topup = True

            if show_traffic_topup:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=texts.t('BUY_TRAFFIC_BUTTON', '📈 Докупить трафик'), callback_data='buy_traffic'
                        )
                    ]
                )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_payment_methods_keyboard_with_cart(
    language: str = 'ru',
    amount_kopeks: int = 0,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = get_payment_methods_keyboard(amount_kopeks, language)

    # Добавляем кнопку "Очистить корзину"
    keyboard.inline_keyboard.append(
        [InlineKeyboardButton(text='🗑️ Очистить корзину и вернуться', callback_data='clear_saved_cart')]
    )

    # Добавляем кнопку возврата к оформлению подписки
    keyboard.inline_keyboard.insert(
        -1,
        [  # Вставляем перед кнопкой "назад"
            InlineKeyboardButton(text=texts.RETURN_TO_SUBSCRIPTION_CHECKOUT, callback_data='return_to_saved_cart')
        ],
    )

    return keyboard


def get_subscription_confirm_keyboard_with_cart(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Подтвердить покупку', callback_data='subscription_confirm')],
            [InlineKeyboardButton(text='🗑️ Очистить корзину', callback_data='clear_saved_cart')],
            [
                InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data='subscription_config_back',  # Изменили на возврат к настройке
                )
            ],
        ]
    )


def get_insufficient_balance_keyboard_with_cart(
    language: str = 'ru',
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
                text='🗑️ Очистить корзину и вернуться',
                callback_data='clear_saved_cart',
            )
        ],
    )

    return keyboard


def get_trial_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('TRIAL_ACTIVATE_BUTTON', '🎁 Активировать'), callback_data='trial_activate'
                ),
                InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu'),
            ]
        ]
    )


def get_subscription_period_keyboard(
    language: str = DEFAULT_LANGUAGE, user: User | None = None
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
        price_info = calculate_user_price(user, base_price, days, 'period')

        # Format period description
        period_display = format_period_description(days, language)

        # Format button text with discount display
        button_text = format_price_button(
            period_label=period_display,
            price_info=price_info,
            format_price_func=texts.format_price,
            emphasize=False,
            add_exclamation=False,
        )

        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f'period_{days}')])

    # Кнопка "Простая покупка" была убрана из выбора периода подписки

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_traffic_packages_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    from app.config import settings

    if settings.is_traffic_topup_blocked():
        return get_back_keyboard(language)

    logger.info('🔍 RAW CONFIG', TRAFFIC_PACKAGES_CONFIG=settings.TRAFFIC_PACKAGES_CONFIG)

    all_packages = settings.get_traffic_packages()
    logger.info('🔍 ALL PACKAGES', all_packages=all_packages)

    enabled_packages = [pkg for pkg in all_packages if pkg['enabled']]
    disabled_packages = [pkg for pkg in all_packages if not pkg['enabled']]

    logger.info('🔍 ENABLED: packages', enabled_packages_count=len(enabled_packages))
    logger.info('🔍 DISABLED: packages', disabled_packages_count=len(disabled_packages))

    for pkg in disabled_packages:
        logger.info('🔍 DISABLED PACKAGE: kopeks, enabled', pkg=pkg['gb'], pkg_2=pkg['price'], pkg_3=pkg['enabled'])

    texts = get_texts(language)
    keyboard = []

    traffic_packages = settings.get_traffic_packages()

    for package in traffic_packages:
        gb = package['gb']
        package['price']
        enabled = package['enabled']

        if not enabled:
            continue

        if gb == 0:
            text = f'♾️ Безлимит - {settings.format_price(package["price"])}'
        else:
            text = f'📊 {gb} ГБ - {settings.format_price(package["price"])}'

        keyboard.append([InlineKeyboardButton(text=text, callback_data=f'traffic_{gb}')])

    if not keyboard:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('TRAFFIC_PACKAGES_NOT_CONFIGURED', '⚠️ Пакеты трафика не настроены'),
                    callback_data='no_traffic_packages',
                )
            ]
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='subscription_config_back')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_countries_keyboard(
    countries: list[dict], selected: list[str], language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    for country in countries:
        if not country.get('is_available', True):
            continue

        emoji = '✅' if country['uuid'] in selected else '⚪'

        if country['price_kopeks'] > 0:
            price_text = f' (+{texts.format_price(country["price_kopeks"])})'
        else:
            price_text = ' (Бесплатно)'

        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f'{emoji} {country["name"]}{price_text}', callback_data=f'country_{country["uuid"]}'
                )
            ]
        )

    if not keyboard:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('NO_SERVERS_AVAILABLE', '❌ Нет доступных серверов'), callback_data='no_servers'
                )
            ]
        )

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=texts.t('CONTINUE_BUTTON', '✅ Продолжить'), callback_data='countries_continue'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data='subscription_config_back')],
        ]
    )

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
        price_text = f' (+{texts.format_price(price)})' if price > 0 else ' (вкл.)'
        emoji = '✅' if devices == current else '⚪'

        button_text = f'{emoji} {devices}{price_text}'

        buttons.append(InlineKeyboardButton(text=button_text, callback_data=f'devices_{devices}'))

    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            keyboard.append([buttons[i], buttons[i + 1]])
        else:
            keyboard.append([buttons[i]])

    keyboard.extend(
        [
            [InlineKeyboardButton(text=texts.t('CONTINUE_BUTTON', '✅ Продолжить'), callback_data='devices_continue')],
            [InlineKeyboardButton(text=texts.BACK, callback_data='subscription_config_back')],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _get_device_declension(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return 'устройство'
    if count % 10 in [2, 3, 4] and count % 100 not in [12, 13, 14]:
        return 'устройства'
    return 'устройств'


def get_subscription_confirm_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.CONFIRM, callback_data='subscription_confirm'),
                InlineKeyboardButton(text=texts.CANCEL, callback_data='subscription_cancel'),
            ]
        ]
    )


def get_balance_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    keyboard = [
        [
            InlineKeyboardButton(text=texts.BALANCE_HISTORY, callback_data='balance_history'),
            InlineKeyboardButton(text=texts.BALANCE_TOP_UP, callback_data='balance_topup'),
        ],
    ]
    if settings.YOOKASSA_RECURRENT_ENABLED:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('SAVED_CARDS_BUTTON', '💳 Привязанные карты'),
                    callback_data='saved_cards_list',
                )
            ]
        )
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_payment_methods_keyboard(amount_kopeks: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    has_direct_payment_methods = False

    amount_kopeks = max(0, int(amount_kopeks or 0))

    def _build_callback(method: str) -> str:
        if amount_kopeks > 0:
            return f'topup_amount|{method}|{amount_kopeks}'
        return f'topup_{method}'

    if settings.TELEGRAM_STARS_ENABLED:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_TELEGRAM_STARS', '⭐ Telegram Stars'), callback_data=_build_callback('stars')
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_yookassa_enabled():
        if settings.YOOKASSA_SBP_ENABLED:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('PAYMENT_SBP_YOOKASSA', '🏦 Оплатить по СБП (YooKassa)'),
                        callback_data=_build_callback('yookassa_sbp'),
                    )
                ]
            )
            has_direct_payment_methods = True

        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_CARD_YOOKASSA', '💳 Банковская карта (YooKassa)'),
                    callback_data=_build_callback('yookassa'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.TRIBUTE_ENABLED:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_CARD_TRIBUTE', '💳 Банковская карта (Tribute)'),
                    callback_data=_build_callback('tribute'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_mulenpay_enabled():
        mulenpay_name = settings.get_mulenpay_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t(
                        'PAYMENT_CARD_MULENPAY',
                        '💳 Банковская карта ({mulenpay_name})',
                    ).format(mulenpay_name=mulenpay_name),
                    callback_data=_build_callback('mulenpay'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_wata_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_CARD_WATA', '💳 Банковская карта (WATA)'),
                    callback_data=_build_callback('wata'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_pal24_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_CARD_PAL24', '🏦 СБП (PayPalych)'), callback_data=_build_callback('pal24')
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_platega_enabled() and settings.get_platega_active_methods():
        platega_name = settings.get_platega_display_name()
        if settings.PLATEGA_INLINE_METHODS:
            for method_code in settings.get_platega_active_methods():
                title = settings.get_platega_method_display_title(method_code)
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=f'{title} ({platega_name})',
                            callback_data=_build_callback(f'platega_m{method_code}'),
                        )
                    ]
                )
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('PAYMENT_PLATEGA', f'💳 {platega_name}'),
                        callback_data=_build_callback('platega'),
                    )
                ]
            )
        has_direct_payment_methods = True

    if settings.is_cryptobot_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_CRYPTOBOT', '🪙 Криптовалюта (CryptoBot)'),
                    callback_data=_build_callback('cryptobot'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_heleket_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_HELEKET', '🪙 Криптовалюта (Heleket)'),
                    callback_data=_build_callback('heleket'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_cloudpayments_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_CLOUDPAYMENTS', '💳 Банковская карта (CloudPayments)'),
                    callback_data=_build_callback('cloudpayments'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_freekassa_sbp_enabled():
        sbp_name = settings.get_freekassa_sbp_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_FREEKASSA_SBP', f'📱 {sbp_name}'),
                    callback_data=_build_callback('freekassa_sbp'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_freekassa_card_enabled():
        card_name = settings.get_freekassa_card_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_FREEKASSA_CARD', f'💳 {card_name}'),
                    callback_data=_build_callback('freekassa_card'),
                )
            ]
        )
        has_direct_payment_methods = True

    if (
        settings.is_freekassa_enabled()
        and not settings.is_freekassa_sbp_enabled()
        and not settings.is_freekassa_card_enabled()
    ):
        freekassa_name = settings.get_freekassa_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_FREEKASSA', f'💳 {freekassa_name}'),
                    callback_data=_build_callback('freekassa'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_kassa_ai_sbp_enabled():
        sbp_name = settings.get_kassa_ai_sbp_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_KASSA_AI_SBP', f'📱 {sbp_name}'),
                    callback_data=_build_callback('kassa_ai_sbp'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_kassa_ai_card_enabled():
        card_name = settings.get_kassa_ai_card_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_KASSA_AI_CARD', f'💳 {card_name}'),
                    callback_data=_build_callback('kassa_ai_card'),
                )
            ]
        )
        has_direct_payment_methods = True

    if (
        settings.is_kassa_ai_enabled()
        and not settings.is_kassa_ai_sbp_enabled()
        and not settings.is_kassa_ai_card_enabled()
    ):
        kassa_ai_name = settings.get_kassa_ai_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_KASSA_AI', f'💳 {kassa_ai_name}'), callback_data=_build_callback('kassa_ai')
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_unitpay_enabled():
        unitpay_name = settings.UNITPAY_DISPLAY_NAME
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_UNITPAY', f'💳 {unitpay_name}'),
                    callback_data=_build_callback('unitpay'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_riopay_enabled():
        riopay_name = settings.get_riopay_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_RIOPAY', f'💳 Банковская карта ({riopay_name})'),
                    callback_data=_build_callback('riopay'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_severpay_enabled():
        severpay_name = settings.get_severpay_display_name()
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_SEVERPAY', f'💳 Банковская карта ({severpay_name})'),
                    callback_data=_build_callback('severpay'),
                )
            ]
        )
        has_direct_payment_methods = True

    if settings.is_support_topup_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENT_VIA_SUPPORT', '🛠️ Через поддержку'), callback_data='topup_support'
                )
            ]
        )

    if not keyboard:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENTS_TEMPORARILY_UNAVAILABLE', '⚠️ Способы оплаты временно недоступны'),
                    callback_data='payment_methods_unavailable',
                )
            ]
        )
    elif not has_direct_payment_methods and settings.is_support_topup_enabled():
        keyboard.insert(
            0,
            [
                InlineKeyboardButton(
                    text=texts.t('PAYMENTS_TEMPORARILY_UNAVAILABLE', '⚠️ Способы оплаты временно недоступны'),
                    callback_data='payment_methods_unavailable',
                )
            ],
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_yookassa_payment_keyboard(
    payment_id: str, amount_kopeks: int, confirmation_url: str, language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('PAY_NOW_BUTTON', '💳 Оплатить'), url=confirmation_url)],
            [
                InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_yookassa_status_{payment_id}',
                )
            ],
            [InlineKeyboardButton(text=texts.t('MY_BALANCE_BUTTON', '💰 Мой баланс'), callback_data='menu_balance')],
        ]
    )


def get_autopay_notification_keyboard(subscription_id: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    sub_btn_text = (
        texts.t('MY_SUBSCRIPTIONS_BUTTON', '📱 Мои подписки')
        if settings.is_multi_tariff_enabled()
        else texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка')
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                build_miniapp_or_callback_button(
                    text=texts.t('TOPUP_BALANCE_BUTTON', '💳 Пополнить баланс'), callback_data='balance_topup'
                )
            ],
            [build_miniapp_or_callback_button(text=sub_btn_text, callback_data='menu_subscription')],
        ]
    )


def get_referral_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    keyboard = [
        [
            InlineKeyboardButton(
                text=texts.t('CREATE_INVITE_BUTTON', '📝 Создать приглашение'), callback_data='referral_create_invite'
            )
        ],
        [InlineKeyboardButton(text=texts.t('SHOW_QR_BUTTON', '📱 Показать QR код'), callback_data='referral_show_qr')],
        [
            InlineKeyboardButton(
                text=texts.t('REFERRAL_LIST_BUTTON', '👥 Список рефералов'), callback_data='referral_list'
            )
        ],
        [
            InlineKeyboardButton(
                text=texts.t('REFERRAL_ANALYTICS_BUTTON', '📊 Аналитика'), callback_data='referral_analytics'
            )
        ],
    ]

    # Добавляем кнопку вывода, если включена
    if settings.is_referral_withdrawal_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_BUTTON', '💸 Запросить вывод'),
                    callback_data='referral_withdrawal',
                )
            ]
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])

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
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('CREATE_TICKET_BUTTON', '🎫 Создать тикет'), callback_data='create_ticket'
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton(text=texts.t('MY_TICKETS_BUTTON', '📋 Мои тикеты'), callback_data='my_tickets')]
        )
    # Direct contact
    if contact_enabled and settings.get_support_contact_url():
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('CONTACT_SUPPORT_BUTTON', '💬 Связаться с поддержкой'),
                    url=settings.get_support_contact_url() or 'https://t.me/',
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_pagination_keyboard(
    current_page: int, total_pages: int, callback_prefix: str, language: str = DEFAULT_LANGUAGE
) -> list[list[InlineKeyboardButton]]:
    texts = get_texts(language)
    keyboard = []

    if total_pages > 1:
        row = []

        if current_page > 1:
            row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_PREV', '⬅️'), callback_data=f'{callback_prefix}_page_{current_page - 1}'
                )
            )

        row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_NEXT', '➡️'), callback_data=f'{callback_prefix}_page_{current_page + 1}'
                )
            )

        keyboard.append(row)

    return keyboard


def get_confirmation_keyboard(
    confirm_data: str, cancel_data: str = 'cancel', language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.YES, callback_data=confirm_data),
                InlineKeyboardButton(text=texts.NO, callback_data=cancel_data),
            ]
        ]
    )


def get_autopay_keyboard(language: str = DEFAULT_LANGUAGE, sub_id: int | None = None) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    back_cb = f'sm:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'menu_subscription'
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.t('ENABLE_BUTTON', '✅ Включить'), callback_data='autopay_enable'),
                InlineKeyboardButton(text=texts.t('DISABLE_BUTTON', '❌ Выключить'), callback_data='autopay_disable'),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('AUTOPAY_SET_DAYS_BUTTON', '⚙️ Настроить дни'), callback_data='autopay_set_days'
                )
            ],
            [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)],
        ]
    )


_PAYMENT_METHOD_LOCALE_KEYS: dict[str, tuple[str, str]] = {
    'bank_card': ('PAYMENT_METHOD_BANK_CARD', '💳 Банковская карта'),
    'yoo_money': ('PAYMENT_METHOD_YOO_MONEY', '🟣 ЮMoney'),
    'sberbank': ('PAYMENT_METHOD_SBERBANK', '🟢 СберPay'),
    'tinkoff_bank': ('PAYMENT_METHOD_TINKOFF_BANK', '🟡 Т-Банк'),
    'sbp': ('PAYMENT_METHOD_SBP', '🏦 СБП'),
    'mir_pay': ('PAYMENT_METHOD_MIR_PAY', '🟦 Mir Pay'),
}


def _get_payment_method_display_name(card, language: str = DEFAULT_LANGUAGE) -> str:
    """Локализованное название метода оплаты + реквизиты."""
    texts = get_texts(language)

    # Для банковских карт title уже содержит тип + маску (например "Visa *4444")
    if card.method_type == 'bank_card' or (not card.method_type and card.card_last4):
        if card.title:
            return card.title
        if card.card_last4:
            return f'{card.card_type or "Card"} *{card.card_last4}'

    # Для остальных методов: локализованное название + реквизиты из title
    locale_entry = _PAYMENT_METHOD_LOCALE_KEYS.get(card.method_type)
    if locale_entry:
        key, default = locale_entry
        method_name = texts.t(key, default)
    else:
        method_name = card.method_type or 'Card'

    if card.title:
        return f'{method_name} {card.title}'
    return method_name


def get_saved_cards_keyboard(cards: list, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []
    for card in cards:
        card_label = f'🗑 {_get_payment_method_display_name(card, language)}'
        keyboard.append([InlineKeyboardButton(text=card_label, callback_data=f'unlink_card_{card.id}')])
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_confirm_unlink_keyboard(card_id: int, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('SAVED_CARDS_CONFIRM_YES', '✅ Да, отвязать'),
                    callback_data=f'confirm_unlink_{card_id}',
                ),
                InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'),
                    callback_data='saved_cards_list',
                ),
            ]
        ]
    )


def get_autopay_days_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    for days in [1, 3, 7, 14]:
        keyboard.append(
            [InlineKeyboardButton(text=f'{days} {_get_days_word(days)}', callback_data=f'autopay_days_{days}')]
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='subscription_autopay')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _get_days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return 'день'
    if 2 <= days % 10 <= 4 and not (12 <= days % 100 <= 14):
        return 'дня'
    return 'дней'


# Deprecated: get_extend_subscription_keyboard() was removed.
# Use get_extend_subscription_keyboard_with_prices() instead for personalized pricing.


def get_add_traffic_keyboard(
    language: str = DEFAULT_LANGUAGE,
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
    sub_id: int | None = None,
) -> InlineKeyboardMarkup:
    from app.config import settings

    texts = get_texts(language)
    language_code = (language or DEFAULT_LANGUAGE).split('-')[0].lower()
    use_russian_fallback = language_code in {'ru', 'fa'}
    back_cb = f'sm:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'menu_subscription'

    # Считаем по дням (как в кабинете и подтверждении)
    if subscription_end_date:
        now = datetime.now(UTC)
        days_left = max(1, (subscription_end_date - now).days)
        price_multiplier = days_left / 30
        period_text = f' (за {days_left} дн.)' if days_left > 1 else ' (за 1 день)'
    else:
        price_multiplier = 1
        period_text = ''

    packages = settings.get_traffic_topup_packages()
    enabled_packages = [pkg for pkg in packages if pkg['enabled'] and pkg['price'] > 0]

    if not enabled_packages:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('NO_TRAFFIC_PACKAGES', '❌ Нет доступных пакетов'),
                        callback_data='no_traffic_packages',
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)],
            ]
        )

    buttons = []

    for package in enabled_packages:
        gb = package['gb']
        price_per_month = package['price']
        discounted_per_month, discount_per_month = apply_percentage_discount(
            price_per_month,
            discount_percent,
        )
        total_price = int(discounted_per_month * price_multiplier)
        total_price = max(100, total_price) if total_price > 0 else 0
        total_discount = int(discount_per_month * price_multiplier)

        if gb == 0:
            if use_russian_fallback:
                text = f'♾️ Безлимитный трафик - {total_price // 100} ₽{period_text}'
            else:
                text = f'♾️ Unlimited traffic - {total_price // 100} ₽{period_text}'
        elif use_russian_fallback:
            text = f'📊 +{gb} ГБ трафика - {total_price // 100} ₽{period_text}'
        else:
            text = f'📊 +{gb} GB traffic - {total_price // 100} ₽{period_text}'

        if discount_percent > 0 and total_discount > 0:
            if use_russian_fallback:
                text += f' (скидка {discount_percent}%: -{total_discount // 100}₽)'
            else:
                text += f' (discount {discount_percent}%: -{total_discount // 100}₽)'

        buttons.append([InlineKeyboardButton(text=text, callback_data=f'add_traffic_{gb}')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_add_traffic_keyboard_from_tariff(
    language: str,
    packages: dict,  # {gb: price_kopeks}
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
    sub_id: int | None = None,
) -> InlineKeyboardMarkup:
    """
    Клавиатура для докупки трафика из настроек тарифа.

    Args:
        language: Язык интерфейса
        packages: Словарь {ГБ: цена_в_копейках} из тарифа
        subscription_end_date: Дата окончания подписки для расчета цены
        discount_percent: Процент скидки
        sub_id: ID подписки для формирования обратной ссылки в multi-tariff режиме
    """
    texts = get_texts(language)
    language_code = (language or DEFAULT_LANGUAGE).split('-')[0].lower()
    use_russian_fallback = language_code in {'ru', 'fa'}
    back_cb = f'sm:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'menu_subscription'

    if not packages:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('NO_TRAFFIC_PACKAGES', '❌ Нет доступных пакетов'),
                        callback_data='no_traffic_packages',
                    )
                ],
                [InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)],
            ]
        )

    buttons = []

    # Сортируем пакеты по размеру, исключаем пакеты с нулевой ценой
    sorted_packages = sorted(((gb, p) for gb, p in packages.items() if p > 0), key=lambda x: x[0])

    # Пакеты трафика на тарифах покупаются на 1 месяц (30 дней),
    # цена в тарифе уже месячная — не умножаем на оставшиеся месяцы подписки
    for gb, price_per_month in sorted_packages:
        discounted_price, discount_value = apply_percentage_discount(
            price_per_month,
            discount_percent,
        )

        period_text = ' /мес' if use_russian_fallback else ' /mo'

        if use_russian_fallback:
            text = f'📊 +{gb} ГБ трафика - {discounted_price // 100} ₽{period_text}'
        else:
            text = f'📊 +{gb} GB traffic - {discounted_price // 100} ₽{period_text}'

        if discount_percent > 0 and discount_value > 0:
            if use_russian_fallback:
                text += f' (скидка {discount_percent}%: -{discount_value // 100}₽)'
            else:
                text += f' (discount {discount_percent}%: -{discount_value // 100}₽)'

        buttons.append([InlineKeyboardButton(text=text, callback_data=f'add_traffic_{gb}')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_change_devices_keyboard(
    current_devices: int,
    language: str = DEFAULT_LANGUAGE,
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
    tariff=None,  # Тариф для цены за устройство
    back_callback: str = 'subscription_settings',
) -> InlineKeyboardMarkup:
    from app.config import settings

    texts = get_texts(language)

    # Считаем по дням (как в кабинете и подтверждении)
    if subscription_end_date:
        now = datetime.now(UTC)
        days_left = max(1, (subscription_end_date - now).days)
        price_multiplier = days_left / 30
        period_text = f' (за {days_left} дн.)' if days_left > 1 else ' (за 1 день)'
    else:
        price_multiplier = 1
        period_text = ''

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

    # Используем max_device_limit из тарифа если есть, иначе глобальную настройку
    tariff_max_devices = getattr(tariff, 'max_device_limit', None) if tariff else None
    if tariff_max_devices and tariff_max_devices > 0:
        max_devices = tariff_max_devices
    else:
        max_devices = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else 20

    # Минимальное количество устройств: device_limit тарифа или 1
    tariff_min_devices = (getattr(tariff, 'device_limit', 1) or 1) if tariff else 1
    min_devices = max(1, tariff_min_devices)

    start_range = max(min_devices, min(current_devices - 3, max_devices - 6))
    end_range = min(max_devices + 1, max(current_devices + 4, 7))

    for devices_count in range(start_range, end_range):
        if devices_count == current_devices:
            emoji = '✅'
            action_text = ' (текущее)'
            price_text = ''
        elif devices_count > current_devices:
            emoji = '➕'
            devices_count - current_devices

            current_chargeable = max(0, current_devices - default_device_limit)
            new_chargeable = max(0, devices_count - default_device_limit)
            chargeable_devices = new_chargeable - current_chargeable

            if chargeable_devices > 0:
                price_per_month = chargeable_devices * device_price_per_month
                discounted_per_month, discount_per_month = apply_percentage_discount(
                    price_per_month,
                    discount_percent,
                )
                total_price = int(discounted_per_month * price_multiplier)
                total_price = max(100, total_price)  # Минимум 1 рубль
                price_text = f' (+{total_price // 100}₽{period_text})'
                total_discount = int(discount_per_month * price_multiplier)
                if discount_percent > 0 and total_discount > 0:
                    price_text += f' (скидка {discount_percent}%: -{total_discount // 100}₽)'
                action_text = ''
            else:
                price_text = ' (бесплатно)'
                action_text = ''
        else:
            emoji = '➖'
            action_text = ''
            price_text = ' (без возврата)'

        button_text = f'{emoji} {devices_count} устр.{action_text}{price_text}'

        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f'change_devices_{devices_count}')])

    if current_devices < start_range or current_devices >= end_range:
        current_button = f'✅ {current_devices} устр. (текущее)'
        buttons.insert(
            0, [InlineKeyboardButton(text=current_button, callback_data=f'change_devices_{current_devices}')]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirm_change_devices_keyboard(
    new_devices_count: int,
    price: int,
    language: str = DEFAULT_LANGUAGE,
    back_callback: str = 'subscription_settings',
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('CONFIRM_CHANGE_BUTTON', '✅ Подтвердить изменение'),
                    callback_data=f'confirm_change_devices_{new_devices_count}_{price}',
                )
            ],
            [InlineKeyboardButton(text=texts.CANCEL, callback_data=back_callback)],
        ]
    )


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
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'✅ Сбросить за {settings.format_price(price_kopeks)}', callback_data='confirm_reset_traffic'
                )
            ]
        )
    else:
        # Не хватает средств - показываем кнопку пополнения
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('TOPUP_BALANCE_BUTTON', '💳 Пополнить баланс'),
                    callback_data='balance_topup',
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text=texts.BACK,
                callback_data='subscription_settings',
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_manage_countries_keyboard(
    countries: list[dict],
    selected: list[str],
    current_subscription_countries: list[str],
    language: str = DEFAULT_LANGUAGE,
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
    sub_id: int | None = None,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    back_cb = f'sm:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'menu_subscription'

    # Считаем по дням (как в кабинете и подтверждении)
    if subscription_end_date:
        now = datetime.now(UTC)
        days_left = max(1, (subscription_end_date - now).days)
        price_multiplier = days_left / 30
        logger.info(
            '🔍 Расчет для управления странами: осталось дней до',
            days_left=days_left,
            subscription_end_date=subscription_end_date,
        )
    else:
        price_multiplier = 1
        days_left = 30

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
                icon = '✅'
            else:
                icon = '➖'
        elif uuid in selected:
            icon = '➕'
            total_cost += int(discounted_per_month * price_multiplier)
        else:
            icon = '⚪'

        if uuid not in current_subscription_countries and uuid in selected:
            total_price = int(discounted_per_month * price_multiplier)
            total_price = max(100, total_price) if total_price > 0 else 0
            if days_left > 30:
                price_text = f' ({discounted_per_month // 100}₽/мес × {days_left} дн. = {total_price // 100}₽)'
                logger.info(
                    '🔍 Сервер : ₽/мес × дн./30 = ₽ (скидка ₽)',
                    name=name,
                    discounted_per_month=discounted_per_month / 100,
                    days_left=days_left,
                    total_price=total_price / 100,
                    discount_per_month=int(discount_per_month * price_multiplier) / 100,
                )
            else:
                price_text = f' ({total_price // 100}₽)'
            total_discount_for_server = int(discount_per_month * price_multiplier)
            if discount_percent > 0 and total_discount_for_server > 0:
                price_text += f' (скидка {discount_percent}%: -{total_discount_for_server // 100}₽)'
            display_name = f'{icon} {name}{price_text}'
        else:
            display_name = f'{icon} {name}'

        buttons.append([InlineKeyboardButton(text=display_name, callback_data=f'country_manage_{uuid}')])

    if total_cost > 0:
        apply_text = f'✅ Применить изменения ({total_cost // 100} ₽)'
        logger.info('🔍 Общая стоимость новых серверов: ₽', total_cost=total_cost / 100)
    else:
        apply_text = '✅ Применить изменения'

    buttons.append([InlineKeyboardButton(text=apply_text, callback_data='countries_apply')])

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_device_selection_keyboard(
    language: str = DEFAULT_LANGUAGE,
    platforms: list[dict] | None = None,
    sub_id: int | None = None,
) -> InlineKeyboardMarkup:
    from app.config import settings
    from app.handlers.subscription.common import get_localized_value

    texts = get_texts(language)
    back_cb = f'sm:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'menu_subscription'

    keyboard: list[list[InlineKeyboardButton]] = []

    if platforms:
        row: list[InlineKeyboardButton] = []
        for p in platforms:
            display_name = p.get('displayName', p['key'])
            if isinstance(display_name, dict):
                display_name = get_localized_value(display_name, language)
            emoji = p.get('icon_emoji', '📱')
            device_type = p.get('device_type', p['key'])
            btn = InlineKeyboardButton(
                text=f'{emoji} {display_name}',
                callback_data=f'device_guide_{device_type}',
            )
            row.append(btn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    if settings.CONNECT_BUTTON_MODE == 'guide':
        _osl_cb = (
            f'open_subscription_link:{sub_id}'
            if sub_id and settings.is_multi_tariff_enabled()
            else 'open_subscription_link'
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('SHOW_SUBSCRIPTION_LINK', '📋 Показать ссылку подписки'),
                    callback_data=_osl_cb,
                )
            ]
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb)])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_connection_guide_keyboard(
    subscription_url: str,
    app: dict,
    device_type: str,
    language: str = DEFAULT_LANGUAGE,
    has_other_apps: bool = False,
    sub_id: int | None = None,
) -> InlineKeyboardMarkup:
    from app.handlers.subscription.common import create_deep_link, get_localized_value, resolve_button_url

    texts = get_texts(language)
    back_cb = f'sm:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'menu_subscription'

    keyboard: list[list[InlineKeyboardButton]] = []

    for block in app.get('blocks', []):
        if not isinstance(block, dict):
            continue
        for btn in block.get('buttons', []):
            if not isinstance(btn, dict):
                continue
            btn_type = btn.get('type', '')
            btn_text = btn.get('text', {})
            if isinstance(btn_text, dict):
                btn_text = get_localized_value(btn_text, language)
            if not btn_text:
                continue

            btn_url = btn.get('url', '') or btn.get('link', '')
            resolved_url = btn.get('resolvedUrl', '')

            if btn_type == 'externalLink':
                if btn_url:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=f'📥 {btn_text}',
                                url=btn_url,
                                style='primary',
                            )
                        ]
                    )
            elif btn_type == 'subscriptionLink':
                url = resolved_url or resolve_button_url(btn_url, subscription_url)
                deep_link = create_deep_link(app.get('_raw', app), subscription_url)
                final_url = deep_link or url or subscription_url
                if final_url:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                url=final_url,
                                style='success',
                            )
                        ]
                    )
                elif settings.is_happ_cryptolink_mode():
                    _osl_cb = (
                        f'open_subscription_link:{sub_id}'
                        if sub_id and settings.is_multi_tariff_enabled()
                        else 'open_subscription_link'
                    )
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                callback_data=_osl_cb,
                                style='success',
                            )
                        ]
                    )
                else:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'),
                                url=subscription_url,
                                style='success',
                            )
                        ]
                    )
            elif btn_type == 'copyButton':
                url = resolved_url or resolve_button_url(btn_url, subscription_url)
                if url:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                text=f'📋 {btn_text}',
                                url=url,
                            )
                        ]
                    )

    if has_other_apps:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('OTHER_APPS_BUTTON', '📋 Другие приложения'),
                    callback_data=f'app_list_{device_type}',
                )
            ]
        )

    _sc_cb = (
        f'subscription_connect:{sub_id}' if sub_id and settings.is_multi_tariff_enabled() else 'subscription_connect'
    )
    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=texts.t('CHOOSE_ANOTHER_DEVICE', '📱 Выбрать другое устройство'),
                    callback_data=_sc_cb,
                )
            ],
            [InlineKeyboardButton(text=texts.t('BACK_TO_SUBSCRIPTION', '⬅️ К подписке'), callback_data=back_cb)],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_app_selection_keyboard(device_type: str, apps: list, language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    for app in apps:
        app_name = app['name']
        if app.get('isFeatured', False):
            app_name = f'⭐ {app_name}'

        keyboard.append([InlineKeyboardButton(text=app_name, callback_data=f'app_{device_type}_{app["id"]}')])

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text=texts.t('CHOOSE_ANOTHER_DEVICE', '📱 Выбрать другое устройство'),
                    callback_data='subscription_connect',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_SUBSCRIPTION', '⬅️ К подписке'), callback_data='menu_subscription'
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_specific_app_keyboard(
    subscription_url: str,
    app: dict,
    device_type: str,
    language: str = DEFAULT_LANGUAGE,
    sub_id: int | None = None,
) -> InlineKeyboardMarkup:
    # Reuse the connection guide keyboard logic — same buttons, just always shows "Other apps"
    return get_connection_guide_keyboard(
        subscription_url,
        app,
        device_type,
        language,
        has_other_apps=True,
        sub_id=sub_id,
    )


def get_extend_subscription_keyboard_with_prices(language: str, prices: dict) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    available_periods = settings.get_available_renewal_periods()

    for days in available_periods:
        if days not in prices:
            continue

        price_info = prices[days]

        if isinstance(price_info, dict):
            final_price = price_info.get('final')
            original_price = price_info.get('original', 0)
            if final_price is None:
                final_price = price_info.get('original', 0)
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
            base_price=original_price, final_price=final_price, discount_percent=discount_percent
        )

        # Format button using unified system
        button_text = format_price_button(
            period_label=period_display,
            price_info=price_info_obj,
            format_price_func=texts.format_price,
            emphasize=False,
            add_exclamation=False,
        )

        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f'extend_period_{days}')])

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_cryptobot_payment_keyboard(
    payment_id: str,
    local_payment_id: int,
    amount_usd: float,
    asset: str,
    bot_invoice_url: str,
    language: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('PAY_WITH_COINS_BUTTON', '🪙 Оплатить'), url=bot_invoice_url)],
            [
                InlineKeyboardButton(
                    text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                    callback_data=f'check_cryptobot_{local_payment_id}',
                )
            ],
            [InlineKeyboardButton(text=texts.t('MY_BALANCE_BUTTON', '💰 Мой баланс'), callback_data='menu_balance')],
        ]
    )


def get_devices_management_keyboard(
    devices: list[dict],
    pagination,
    language: str = DEFAULT_LANGUAGE,
    back_callback: str = 'subscription_settings',
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    keyboard = []

    for i, device in enumerate(devices):
        platform = device.get('platform', 'Unknown')
        device_model = device.get('deviceModel', 'Unknown')
        device_info = f'{platform} - {device_model}'

        if len(device_info) > 25:
            device_info = device_info[:22] + '...'

        keyboard.append(
            [InlineKeyboardButton(text=f'🔄 {device_info}', callback_data=f'reset_device_{i}_{pagination.page}')]
        )

    if pagination.total_pages > 1:
        nav_row = []

        if pagination.has_prev:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_PREV', '⬅️'), callback_data=f'devices_page_{pagination.prev_page}'
                )
            )

        nav_row.append(
            InlineKeyboardButton(text=f'{pagination.page}/{pagination.total_pages}', callback_data='current_page')
        )

        if pagination.has_next:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_NEXT', '➡️'), callback_data=f'devices_page_{pagination.next_page}'
                )
            )

        keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('RESET_ALL_DEVICES_BUTTON', '🔄 Сбросить все устройства'),
                callback_data='reset_all_devices',
            )
        ]
    )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

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
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADD_COUNTRIES_BUTTON', '🌐 Добавить страны'),
                    callback_data='subscription_add_countries',
                )
            ]
        )

    if settings.is_traffic_selectable() and not has_tariff:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('RESET_TRAFFIC_BUTTON', '🔄 Сбросить трафик'),
                    callback_data='subscription_reset_traffic',
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('SWITCH_TRAFFIC_BUTTON', '🔄 Переключить трафик'),
                    callback_data='subscription_switch_traffic',
                )
            ]
        )

    # Устройства: для тарифов - только если указана цена за устройство
    if has_tariff:
        tariff_device_price = getattr(tariff, 'device_price_kopeks', None)
        if tariff_device_price is not None and tariff_device_price > 0:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=texts.t('CHANGE_DEVICES_BUTTON', '📱 Изменить устройства'),
                        callback_data='subscription_change_devices',
                    )
                ]
            )
    elif settings.is_devices_selection_enabled():
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('CHANGE_DEVICES_BUTTON', '📱 Изменить устройства'),
                    callback_data='subscription_change_devices',
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('MANAGE_DEVICES_BUTTON', '🔧 Управление устройствами'),
                callback_data='subscription_manage_devices',
            )
        ]
    )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_subscription')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_device_reset_confirm_keyboard(
    device_info: str, device_index: int, page: int, language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('RESET_DEVICE_CONFIRM_BUTTON', '✅ Да, сбросить это устройство'),
                    callback_data=f'confirm_reset_device_{device_index}_{page}',
                )
            ],
            [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'devices_page_{page}')],
        ]
    )


def get_device_management_help_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('DEVICE_CONNECTION_HELP', '❓ Как подключить устройство заново?'),
                    callback_data='device_connection_help',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('MANAGE_DEVICES_BUTTON', '🔧 Управление устройствами'),
                    callback_data='subscription_manage_devices',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('BACK_TO_SUBSCRIPTION', '⬅️ К подписке'), callback_data='menu_subscription'
                )
            ],
        ]
    )


# ==================== TICKET KEYBOARDS ====================


def get_ticket_cancel_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('CANCEL_TICKET_CREATION', '❌ Отменить создание тикета'),
                    callback_data='cancel_ticket_creation',
                )
            ]
        ]
    )


def get_my_tickets_keyboard(
    tickets: list[dict],
    current_page: int = 1,
    total_pages: int = 1,
    language: str = DEFAULT_LANGUAGE,
    page_prefix: str = 'my_tickets_page_',
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    for ticket in tickets:
        status_emoji = ticket.get('status_emoji', '❓')
        # Override status emoji for closed tickets in admin list
        if ticket.get('is_closed', False):
            status_emoji = '✅'
        title = ticket.get('title', 'Без названия')[:25]
        button_text = f'{status_emoji} #{ticket["id"]} {title}'

        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f'view_ticket_{ticket["id"]}')])

    # Пагинация
    if total_pages > 1:
        nav_row = []

        if current_page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_PREV', '⬅️'), callback_data=f'{page_prefix}{current_page - 1}'
                )
            )

        nav_row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_NEXT', '➡️'), callback_data=f'{page_prefix}{current_page + 1}'
                )
            )

        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='menu_support')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_ticket_view_keyboard(
    ticket_id: int, is_closed: bool = False, language: str = DEFAULT_LANGUAGE
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if not is_closed:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('REPLY_TO_TICKET', '💬 Ответить'), callback_data=f'reply_ticket_{ticket_id}'
                )
            ]
        )

    if not is_closed:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('CLOSE_TICKET', '🔒 Закрыть тикет'), callback_data=f'close_ticket_{ticket_id}'
                )
            ]
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='my_tickets')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_ticket_reply_cancel_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('CANCEL_REPLY', '❌ Отменить ответ'), callback_data='cancel_ticket_reply'
                )
            ]
        ]
    )


# ==================== ADMIN TICKET KEYBOARDS ====================


def get_admin_tickets_keyboard(
    tickets: list[dict],
    current_page: int = 1,
    total_pages: int = 1,
    language: str = DEFAULT_LANGUAGE,
    scope: str = 'all',
    *,
    back_callback: str = 'admin_submenu_support',
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
            contact_parts.append(f'@{username}')
        if telegram_id:
            contact_parts.append(str(telegram_id))
        if contact_parts:
            name_parts.append(f'({" | ".join(contact_parts)})')
        name_display = ' '.join(name_parts)
        title = ticket.get('title', 'Без названия')[:20]
        locked_emoji = ticket.get('locked_emoji', '')
        button_text = f'{status_emoji} #{ticket["id"]} {locked_emoji} {name_display}: {title}'.replace('  ', ' ')
        row = [InlineKeyboardButton(text=button_text, callback_data=f'admin_view_ticket_{ticket["id"]}')]
        if ticket.get('is_closed', False):
            closed_rows.append(row)
        else:
            open_rows.append(row)

    # Scope switcher
    switch_row = []
    switch_row.append(
        InlineKeyboardButton(text=texts.t('OPEN_TICKETS', '🔴 Открытые'), callback_data='admin_tickets_scope_open')
    )
    switch_row.append(
        InlineKeyboardButton(text=texts.t('CLOSED_TICKETS', '🟢 Закрытые'), callback_data='admin_tickets_scope_closed')
    )
    keyboard.append(switch_row)

    if open_rows and scope in ('all', 'open'):
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_CLOSE_ALL_OPEN_TICKETS', '🔒 Закрыть все открытые'),
                    callback_data='admin_tickets_close_all_open',
                )
            ]
        )
        keyboard.append(
            [InlineKeyboardButton(text=texts.t('OPEN_TICKETS_HEADER', 'Открытые тикеты'), callback_data='noop')]
        )
        keyboard.extend(open_rows)
    if closed_rows and scope in ('all', 'closed'):
        keyboard.append(
            [InlineKeyboardButton(text=texts.t('CLOSED_TICKETS_HEADER', 'Закрытые тикеты'), callback_data='noop')]
        )
        keyboard.extend(closed_rows)

    # Пагинация
    if total_pages > 1:
        nav_row = []

        if current_page > 1:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_PREV', '⬅️'), callback_data=f'admin_tickets_page_{scope}_{current_page - 1}'
                )
            )

        nav_row.append(InlineKeyboardButton(text=f'{current_page}/{total_pages}', callback_data='current_page'))

        if current_page < total_pages:
            nav_row.append(
                InlineKeyboardButton(
                    text=texts.t('PAGINATION_NEXT', '➡️'), callback_data=f'admin_tickets_page_{scope}_{current_page + 1}'
                )
            )

        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_admin_ticket_view_keyboard(
    ticket_id: int, is_closed: bool = False, language: str = DEFAULT_LANGUAGE, *, is_user_blocked: bool = False
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    keyboard = []

    if not is_closed:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('REPLY_TO_TICKET', '💬 Ответить'), callback_data=f'admin_reply_ticket_{ticket_id}'
                )
            ]
        )

    if not is_closed:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('CLOSE_TICKET', '🔒 Закрыть тикет'), callback_data=f'admin_close_ticket_{ticket_id}'
                )
            ]
        )

    # Блок-контролы: когда не заблокирован — показать два варианта, когда заблокирован — только "Разблокировать"
    if is_user_blocked:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('UNBLOCK', '✅ Разблокировать'), callback_data=f'admin_unblock_user_ticket_{ticket_id}'
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('BLOCK_FOREVER', '🚫 Заблокировать'),
                    callback_data=f'admin_block_user_perm_ticket_{ticket_id}',
                ),
                InlineKeyboardButton(
                    text=texts.t('BLOCK_BY_TIME', '⏳ Блок по времени'),
                    callback_data=f'admin_block_user_ticket_{ticket_id}',
                ),
            ]
        )

    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_tickets')])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_admin_ticket_reply_cancel_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('CANCEL_REPLY', '❌ Отменить ответ'), callback_data='cancel_admin_ticket_reply'
                )
            ]
        ]
    )
