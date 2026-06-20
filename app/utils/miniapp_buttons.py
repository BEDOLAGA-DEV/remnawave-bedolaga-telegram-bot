import re

from aiogram import types
from aiogram.types import InlineKeyboardButton

from app.config import settings
from app.utils.button_styles_cache import CALLBACK_TO_SECTION, get_cached_button_styles


# Юникод-диапазоны для одиночного emoji в начале строки + модификаторы (skin tone,
# variation selector, zero-width joiner-цепочки) + опциональный пробел после.
# Используется когда у кнопки задан icon_custom_emoji_id — Telegram сам рендерит
# кастом emoji слева, и если оставить юникод-emoji в тексте, юзер увидит дубль.
_EMOJI_CHAR_CLASS = (
    r'[℀-⅏'  # Letterlike (ℹ ™ ©)
    r'←-⇿'  # Arrows
    r'⌀-⏿'  # Misc Technical (⌚ ⌨ ⏰ etc.)
    r'①-⓿'  # Enclosed Alphanumerics
    r'■-⛿'  # Geometric Shapes + Misc Symbols (☀ ★ ✈ etc.)
    r'✀-➿'  # Dingbats (✂ ✅ ✔ etc.)
    r'⬀-⯿'  # Misc Symbols/Arrows (⬅ ⭐ etc.)
    r'〰-〽'  # CJK swung dash etc.
    r'㊗㊙'  # Japanese marks
    r'\U0001F000-\U0001FFFF'  # Все supplementary planes (emoticons, pictographs, transport, supp symbols)
    r']'
)
_LEADING_EMOJI_RE = re.compile(
    r'^' + _EMOJI_CHAR_CLASS + r'(?:[️‍\U0001F3FB-\U0001F3FF]|' + _EMOJI_CHAR_CLASS + r')*' + r'\s*'
)


def strip_leading_emoji(text: str) -> str:
    """Удалить ведущий юникод-emoji + следующий пробел. Безопасно для текста без emoji."""
    if not text:
        return text
    return _LEADING_EMOJI_RE.sub('', text, count=1)


# Mapping from callback_data to cabinet frontend paths.
# Used for automatic deep-linking when explicit ``cabinet_path`` is not provided.
# If callback_data is NOT in this mapping, the button falls back to a regular callback.
CALLBACK_TO_CABINET_PATH: dict[str, str] = {
    'menu_balance': '/balance',
    'balance_topup': '/balance/top-up',
    'menu_subscription': '/subscription',
    'subscription': '/subscription',
    'subscription_extend': '/subscription',
    'subscription_upgrade': '/subscription',
    'subscription_connect': '/subscription',
    'subscription_resume_checkout': '/subscription',
    'return_to_saved_cart': '/subscription',
    'menu_buy': '/subscription',
    'buy_traffic': '/subscription',
    'menu_referrals': '/referral',
    'menu_referral': '/referral',
    'menu_promocode': '/balance',
    'menu_support': '/support',
    'menu_info': '/info',
    'menu_profile': '/profile',
    # NB: ``back_to_menu`` is intentionally NOT mapped here.
    # The callback semantically means "return to the bot's main menu"
    # — every other call site in the codebase uses raw
    # ``InlineKeyboardButton(callback_data='back_to_menu')`` for it.
    # Routing it through this helper in cabinet mode would silently
    # turn the button into a WebApp launcher that opens the cabinet
    # root, which is NOT what the user-visible label "Главное меню"
    # promises. Without an entry here, the helper falls through to
    # an InlineKeyboardButton with callback_data and the bot's
    # ``back_to_menu`` handler runs as expected.
}

# Default button styles per callback_data for cabinet mode.
# Values: 'primary' (blue), 'success' (green), 'danger' (red), None (default).
CALLBACK_TO_CABINET_STYLE: dict[str, str] = {
    'menu_balance': 'primary',
    'balance_topup': 'primary',
    'menu_subscription': 'success',
    'subscription': 'success',
    'subscription_extend': 'success',
    'subscription_upgrade': 'success',
    'subscription_connect': 'success',
    'subscription_resume_checkout': 'success',
    'return_to_saved_cart': 'success',
    'menu_buy': 'success',
    'buy_traffic': 'success',
    'menu_referrals': 'success',
    'menu_referral': 'success',
    'menu_promocode': 'primary',
    'menu_support': 'primary',
    'menu_info': 'primary',
    'menu_profile': 'primary',
    # See CALLBACK_TO_CABINET_PATH comment — back_to_menu is bot-menu only,
    # not a cabinet-routed action, so styling here is dead config.
}

# Mapping from broadcast button keys to cabinet paths. Used by the
# admin-broadcast custom-button builder in app/handlers/admin/messages.py
# to swap selected buttons for WebApp launchers in cabinet mode.
#
# ``'home'`` is intentionally NOT mapped here — same reason as
# ``back_to_menu`` above. A broadcast button labelled "Home" must
# always be a bot-menu callback regardless of MAIN_MENU_MODE; otherwise
# users tapping it in cabinet mode get stuck in cabinet root with no
# way back to the bot view. The set-membership gate
# ``CABINET_MINIAPP_BUTTON_KEYS`` in admin/messages.py already excludes
# ``'home'``, but removing the foot-gun entry here is the structural fix:
# even if someone adds ``'home'`` to that set in a future commit, the
# mapping lookup falls through to empty string and ``build_miniapp_or_callback_button``
# returns a callback button.
BUTTON_KEY_TO_CABINET_PATH: dict[str, str] = {
    'balance': '/balance/top-up',
    'referrals': '/referral',
    'promocode': '/balance',
    'connect': '/subscription',
    'subscription': '/subscription',
    'support': '/support',
}

# Valid style values accepted by the Telegram Bot API.
_VALID_STYLES = frozenset({'primary', 'success', 'danger'})


def build_main_menu_button(text: str) -> InlineKeyboardButton:
    """Always-callback button for "Main Menu" / "Главное меню" navigation.

    Exists as a typed alternative to ``build_miniapp_or_callback_button``
    for the one case where cabinet-routing is semantically wrong:
    a button explicitly labelled "Главное меню" must return the user
    to the bot's inline menu (``back_to_menu`` callback handler),
    NOT open the cabinet root via WebApp. Otherwise a user in
    ``MAIN_MENU_MODE=cabinet`` who taps "Главное меню" lands back in
    the cabinet they're trying to exit — an infinite loop UX.

    Production incident (2026-05-18): the top-up success notification
    used ``build_miniapp_or_callback_button(callback_data='back_to_menu')``
    which silently routed to cabinet root in cabinet mode. Defense at
    two layers: ``back_to_menu`` is intentionally absent from
    ``CALLBACK_TO_CABINET_PATH`` so the helper falls through to
    callback even if called wrongly, AND this dedicated factory is
    what callers should reach for to express intent.
    """
    return InlineKeyboardButton(text=text, callback_data='back_to_menu')


def _resolve_style(style: str | None) -> str | None:
    """Return a validated style or ``None``."""
    if style and style in _VALID_STYLES:
        return style
    return None


def build_cabinet_url(path: str = '') -> str:
    """Join ``MINIAPP_CUSTOM_URL`` with an optional *path* segment.

    Handles trailing-slash normalization so that both
    ``https://example.com`` and ``https://example.com/`` produce
    correct URLs like ``https://example.com/balance``.

    Returns an empty string when the base URL is not configured
    or when *path* is empty (no known section).
    """
    base = (settings.MINIAPP_CUSTOM_URL or '').strip().rstrip('/')
    if not base:
        return ''
    if not path:
        return ''
    if path == '/':
        return base
    if not path.startswith('/'):
        path = f'/{path}'
    return f'{base}{path}'


def build_miniapp_or_callback_button(
    text: str,
    *,
    callback_data: str,
    cabinet_path: str | None = None,
    style: str | None = None,
    icon_custom_emoji_id: str | None = None,
) -> InlineKeyboardButton:
    """Create a button that opens the cabinet miniapp or falls back to a callback.

    In cabinet menu mode, if ``MINIAPP_CUSTOM_URL`` is configured the button
    opens the relevant section of the cabinet.  The target section is determined
    by ``cabinet_path`` (explicit) or inferred from ``callback_data`` via
    ``CALLBACK_TO_CABINET_PATH``.

    Button styling (Bot API 9.4):
    - ``style`` overrides the button color: ``'primary'`` (blue),
      ``'success'`` (green), ``'danger'`` (red).  When omitted the style is
      resolved from ``CABINET_BUTTON_STYLE`` config or per-section defaults.
    - ``icon_custom_emoji_id`` shows a custom emoji before the button text
      (requires bot owner to have Telegram Premium).

    When ``callback_data`` is not found in the mapping and no explicit
    ``cabinet_path`` is given, the button falls back to a regular Telegram
    callback — this keeps actions like ``claim_discount_*`` working correctly.

    Only ``MINIAPP_CUSTOM_URL`` is considered here — the purchase-only URL
    (``MINIAPP_PURCHASE_URL``) is intentionally excluded because it cannot
    display subscription details and would load indefinitely.
    """

    if settings.is_cabinet_mode():
        path = cabinet_path or CALLBACK_TO_CABINET_PATH.get(callback_data)
        if path:
            url = build_cabinet_url(path)
            if url:
                # Resolve per-section config from cache
                section = CALLBACK_TO_SECTION.get(callback_data)
                section_cfg = get_cached_button_styles().get(section or '', {}) if section else {}

                # Style chain: explicit param > per-section DB > global config > hardcoded default
                # 'default' in per-section config means "no color" — do not fall through.
                if style:
                    resolved_style = _resolve_style(style)
                elif section_cfg.get('style'):
                    resolved_style = _resolve_style(section_cfg['style'])
                else:
                    resolved_style = _resolve_style((settings.CABINET_BUTTON_STYLE or '').strip()) or _resolve_style(
                        CALLBACK_TO_CABINET_STYLE.get(callback_data)
                    )

                # Emoji chain: explicit param > per-section DB
                resolved_emoji = icon_custom_emoji_id or section_cfg.get('icon_custom_emoji_id') or None

                # Если есть кастом emoji — стрипаем ведущий юникод-эмодзи из текста,
                # иначе у юзера будут две иконки слева (custom + default).
                final_text = strip_leading_emoji(text) if resolved_emoji else text

                return InlineKeyboardButton(
                    text=final_text,
                    web_app=types.WebAppInfo(url=url),
                    style=resolved_style,
                    icon_custom_emoji_id=resolved_emoji or None,
                )

    return InlineKeyboardButton(text=text, callback_data=callback_data)


# Префикс startapp/маршрута для диплинка на конкретный тикет в админ-кабинете.
# Должен совпадать с разбором на стороне фронта (bedolaga-cabinet): start_param
# 'admin_ticket_<id>' и маршрут '/admin/tickets/<id>'.
ADMIN_TICKET_DEEPLINK_PREFIX = 'admin_ticket_'


def build_miniapp_startapp_url(start_param: str) -> str:
    """Собрать t.me Mini App deep link, открывающий кабинет в ЛЮБОМ типе чата.

    ``https://t.me/<bot>/<app>?startapp=<start_param>`` — работает и в группах/
    каналах, где ``web_app``-кнопки недоступны. Требует и имя бота, и
    зарегистрированное короткое имя Mini App (``MINIAPP_APP_SHORT_NAME``,
    BotFather → /newapp). Возвращает '' если чего-то не хватает.
    """
    bot_username = settings.get_bot_username()
    app_name = (getattr(settings, 'MINIAPP_APP_SHORT_NAME', '') or '').strip()
    if not bot_username or not app_name:
        return ''
    return f'https://t.me/{bot_username}/{app_name}?startapp={start_param}'


def build_admin_ticket_cabinet_button(
    ticket_id: int,
    *,
    text: str,
    in_group: bool,
) -> InlineKeyboardButton | None:
    """Кнопка «открыть тикет в админ-кабинете» для уведомления о тикете.

    Строится только в cabinet-режиме (``is_cabinet_mode``):
    - личный чат (``in_group=False``) → ``web_app`` на ``/admin/tickets/<id>``
      (Telegram сам прокидывает initData → кабинет авторизует и роутит к тикету);
    - группа/канал (``in_group=True``) → ``web_app`` недоступен, поэтому t.me
      Mini App startapp-диплинк (нужен ``MINIAPP_APP_SHORT_NAME``).

    Возвращает ``None``, если подходящую кнопку построить нельзя (не cabinet-режим,
    не задан URL кабинета, или в группе не зарегистрирован Mini App).
    """
    if not settings.is_cabinet_mode():
        return None

    if in_group:
        url = build_miniapp_startapp_url(f'{ADMIN_TICKET_DEEPLINK_PREFIX}{ticket_id}')
        if not url:
            return None
        return InlineKeyboardButton(text=text, url=url)

    url = build_cabinet_url(f'/admin/tickets/{ticket_id}')
    if not url:
        return None
    return InlineKeyboardButton(text=text, web_app=types.WebAppInfo(url=url))
