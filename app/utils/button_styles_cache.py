"""Lightweight in-process cache for per-section cabinet button styles.

Avoids circular imports between ``cabinet.routes`` and ``app.utils.miniapp_buttons``
by keeping the cache and its helpers in a dedicated module.
"""

import json

import structlog

from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

# ---- Defaults per section ------------------------------------------------

DEFAULT_BUTTON_STYLES: dict[str, dict] = {
    # Cabinet web UI sections
    'home': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'subscription': {'style': 'success', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'balance': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'referral': {'style': 'success', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'support': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'info': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'admin': {'style': 'danger', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'language': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    # Additional bot menu buttons (mirrors BUILTIN_BUTTONS_INFO in
    # app/services/menu_layout/constants.py).
    'connect': {'style': 'success', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'happ_download': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'buy_traffic': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'trial': {'style': 'success', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'buy_subscription': {'style': 'success', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'simple_subscription': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'resume_checkout': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'promocode': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'referrals': {'style': 'success', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'contests': {'style': 'primary', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'admin_panel': {'style': 'danger', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
    'moderator_panel': {'style': 'danger', 'icon_custom_emoji_id': '', 'enabled': True, 'labels': {}},
}

BOT_LOCALES = ('ru', 'en', 'ua', 'zh', 'fa')

SECTIONS = list(DEFAULT_BUTTON_STYLES.keys())

# Map callback_data values to their logical section name.
CALLBACK_TO_SECTION: dict[str, str] = {
    'menu_profile_unavailable': 'home',
    'back_to_menu': 'home',
    'menu_subscription': 'subscription',
    'subscription': 'subscription',
    'subscription_extend': 'subscription',
    'subscription_upgrade': 'subscription',
    'subscription_connect': 'connect',
    'subscription_happ_download': 'happ_download',
    'subscription_resume_checkout': 'resume_checkout',
    'return_to_saved_cart': 'resume_checkout',
    'menu_buy': 'buy_subscription',
    'simple_subscription_purchase': 'simple_subscription',
    'buy_traffic': 'buy_traffic',
    'menu_balance': 'balance',
    'balance_topup': 'balance',
    'menu_referrals': 'referrals',
    'menu_referral': 'referral',
    'menu_support': 'support',
    'menu_info': 'info',
    'menu_language': 'language',
    'menu_trial': 'trial',
    'menu_promocode': 'promocode',
    'contests_menu': 'contests',
    'admin_panel': 'admin_panel',
    'moderator_panel': 'moderator_panel',
}

# DB key used for storage.
BUTTON_STYLES_KEY = 'CABINET_BUTTON_STYLES'

# Valid Telegram Bot API style values.
VALID_STYLES = frozenset({'primary', 'success', 'danger'})

# All style values accepted by the admin API ('default' = no color, Telegram default).
ALLOWED_STYLE_VALUES = VALID_STYLES | {'default'}

# ---- Module-level cache ---------------------------------------------------

_cached_styles: dict[str, dict] | None = None


def _deep_copy_styles(source: dict[str, dict]) -> dict[str, dict]:
    """Return a deep copy of styles dict (copies nested ``labels`` dicts)."""
    return {section: {**cfg, 'labels': dict(cfg.get('labels', {}))} for section, cfg in source.items()}


def get_cached_button_styles() -> dict[str, dict]:
    """Return the current merged config (DB overrides + defaults).

    If the cache has not been loaded yet, returns defaults.
    """
    if _cached_styles is not None:
        return _deep_copy_styles(_cached_styles)
    return _deep_copy_styles(DEFAULT_BUTTON_STYLES)


async def load_button_styles_cache() -> dict[str, dict]:
    """Load button styles from DB and refresh the module cache.

    Called at bot startup and after admin updates via the cabinet API.
    """
    global _cached_styles

    merged = _deep_copy_styles(DEFAULT_BUTTON_STYLES)

    try:
        from sqlalchemy import select

        from app.database.models import SystemSetting

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SystemSetting).where(SystemSetting.key == BUTTON_STYLES_KEY))
            setting = result.scalar_one_or_none()
            if setting and setting.value:
                db_data: dict = json.loads(setting.value)
                for section, overrides in db_data.items():
                    if section in merged and isinstance(overrides, dict):
                        if overrides.get('style') in ALLOWED_STYLE_VALUES:
                            merged[section]['style'] = overrides['style']
                        if isinstance(overrides.get('icon_custom_emoji_id'), str):
                            merged[section]['icon_custom_emoji_id'] = overrides['icon_custom_emoji_id']
                        if isinstance(overrides.get('enabled'), bool):
                            merged[section]['enabled'] = overrides['enabled']
                        if isinstance(overrides.get('labels'), dict):
                            merged[section]['labels'] = {
                                k: v
                                for k, v in overrides['labels'].items()
                                if isinstance(k, str) and isinstance(v, str) and k in BOT_LOCALES
                            }
    except Exception:
        logger.exception('Failed to load button styles from DB, using defaults')

    _cached_styles = merged
    logger.info('Button styles cache loaded', list=list(merged.keys()))
    return merged
