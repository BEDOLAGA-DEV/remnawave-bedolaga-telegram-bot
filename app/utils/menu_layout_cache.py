"""Lightweight in-process cache for cabinet menu row layout configuration.

Stores per-row button arrangement (which buttons per row, max_per_row)
and custom URL buttons. Loaded from SystemSetting key ``CABINET_MENU_LAYOUT``.
"""

import json

import structlog

from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)

# ---- Constants ---------------------------------------------------------------

MENU_LAYOUT_KEY = 'CABINET_MENU_LAYOUT'

BUILTIN_SECTIONS: tuple[str, ...] = (
    'home',
    'subscription',
    'balance',
    'referral',
    'support',
    'info',
    'admin',
    'language',
)

VALID_MAX_PER_ROW = frozenset({1, 2, 3})

# Valid Telegram Bot API style values for custom buttons.
VALID_CUSTOM_BUTTON_STYLES = frozenset({'primary', 'success', 'danger', 'default'})

DEFAULT_MENU_LAYOUT: dict[str, object] = {
    'row_1': {'id': 'row_1', 'buttons': ['home'], 'max_per_row': 1},
    'row_2': {'id': 'row_2', 'buttons': ['subscription', 'balance'], 'max_per_row': 2},
    'row_3': {'id': 'row_3', 'buttons': ['referral', 'support'], 'max_per_row': 2},
    'row_4': {'id': 'row_4', 'buttons': ['info', 'language'], 'max_per_row': 2},
    'row_5': {'id': 'row_5', 'buttons': ['admin'], 'max_per_row': 1},
    'custom_buttons': {},
}

# ---- Module-level cache ------------------------------------------------------

_cached_layout: dict[str, object] | None = None


def _deep_copy_layout(source: dict[str, object]) -> dict[str, object]:
    """Return a deep copy of layout dict via JSON round-trip."""
    return json.loads(json.dumps(source))


def get_cached_menu_layout() -> dict[str, object]:
    """Return the current layout config (DB overrides + defaults).

    If the cache has not been loaded yet, returns defaults.
    """
    if _cached_layout is not None:
        return _deep_copy_layout(_cached_layout)
    return _deep_copy_layout(DEFAULT_MENU_LAYOUT)


def _validate_row(row_id: str, data: dict) -> dict | None:
    """Validate and sanitize a single row entry. Returns cleaned dict or None."""
    if not isinstance(data, dict):
        return None

    buttons = data.get('buttons')
    if not isinstance(buttons, list) or not buttons:
        return None

    # Allow known built-in section names AND custom_* button IDs in rows
    clean_buttons = [b for b in buttons if isinstance(b, str) and (b in BUILTIN_SECTIONS or b.startswith('custom_'))]
    if not clean_buttons:
        return None

    max_per_row = data.get('max_per_row')
    if not isinstance(max_per_row, int) or max_per_row not in VALID_MAX_PER_ROW:
        max_per_row = 1

    return {'id': row_id, 'buttons': clean_buttons, 'max_per_row': max_per_row}


def _validate_custom_button(btn_id: str, data: dict) -> dict | None:
    """Validate and sanitize a single custom URL button. Returns cleaned dict or None."""
    if not isinstance(data, dict):
        return None
    if not btn_id.startswith('custom_'):
        return None

    url = data.get('url')
    if not isinstance(url, str) or not url.strip():
        return None

    style = data.get('style', 'primary')
    if style not in VALID_CUSTOM_BUTTON_STYLES:
        style = 'primary'

    labels = data.get('labels')
    if not isinstance(labels, dict):
        labels = {}
    clean_labels = {k: v for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)}

    icon_custom_emoji_id = data.get('icon_custom_emoji_id', '')
    if not isinstance(icon_custom_emoji_id, str):
        icon_custom_emoji_id = ''

    enabled = data.get('enabled', True)
    if not isinstance(enabled, bool):
        enabled = True

    open_in = data.get('open_in', 'external')
    if open_in not in ('external', 'webapp'):
        open_in = 'external'
    if open_in == 'webapp' and not url.strip().startswith('https://'):
        open_in = 'external'

    return {
        'id': btn_id,
        'url': url.strip(),
        'style': style,
        'labels': clean_labels,
        'icon_custom_emoji_id': icon_custom_emoji_id,
        'enabled': enabled,
        'open_in': open_in,
    }


def _validate_layout(data: dict) -> dict[str, object]:
    """Validate and sanitize full layout data from DB.

    Returns a clean layout dict; invalid entries are silently dropped.
    """
    result: dict[str, object] = {}

    for key, value in data.items():
        if key == 'custom_buttons':
            if isinstance(value, dict):
                clean_customs: dict[str, dict] = {}
                for btn_id, btn_data in value.items():
                    validated = _validate_custom_button(str(btn_id), btn_data)
                    if validated is not None:
                        clean_customs[str(btn_id)] = validated
                result['custom_buttons'] = clean_customs
        elif key.startswith('row_'):
            validated_row = _validate_row(key, value)
            if validated_row is not None:
                result[key] = validated_row

    # Ensure custom_buttons key always exists
    if 'custom_buttons' not in result:
        result['custom_buttons'] = {}

    return result


async def load_menu_layout_cache() -> dict[str, object]:
    """Load menu layout from DB and refresh the module cache.

    Called at bot startup and after admin updates via the cabinet API.
    """
    global _cached_layout

    merged = _deep_copy_layout(DEFAULT_MENU_LAYOUT)

    try:
        from sqlalchemy import select

        from app.database.models import SystemSetting

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SystemSetting).where(SystemSetting.key == MENU_LAYOUT_KEY))
            setting = result.scalar_one_or_none()
            if setting and setting.value:
                db_data: dict = json.loads(setting.value)
                if isinstance(db_data, dict):
                    validated = _validate_layout(db_data)
                    if validated and any(k.startswith('row_') for k in validated):
                        # Replace rows and custom_buttons from DB only if at least one row exists
                        merged = validated
                        # Ensure custom_buttons always present
                        if 'custom_buttons' not in merged:
                            merged['custom_buttons'] = {}
    except Exception:
        logger.exception('Failed to load menu layout from DB, using defaults')

    _cached_layout = merged
    logger.info('Menu layout cache loaded', rows=len([k for k in merged if k.startswith('row_')]))
    return merged
