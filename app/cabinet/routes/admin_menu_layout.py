"""Admin routes for cabinet menu layout configuration (rows + custom URL buttons).

Serves a MERGED view combining ``CABINET_MENU_LAYOUT`` (row arrangement, custom buttons)
and ``CABINET_BUTTON_STYLES`` (per-section style/emoji/enabled/labels) to the frontend.
On save, splits the payload back into two SystemSetting keys.
"""

import json
import re
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.utils.button_styles_cache import (
    ALLOWED_STYLE_VALUES,
    BOT_LOCALES,
    BUTTON_STYLES_KEY,
    DEFAULT_BUTTON_STYLES,
    get_cached_button_styles,
    load_button_styles_cache,
)
from app.utils.menu_layout_cache import (
    BUILTIN_SECTIONS,
    DEFAULT_MENU_LAYOUT,
    MENU_LAYOUT_KEY,
    VALID_CUSTOM_BUTTON_STYLES,
    get_cached_menu_layout,
    load_menu_layout_cache,
)

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/menu-layout', tags=['Admin Menu Layout'])

# ---- Constants ---------------------------------------------------------------

MAX_ROWS = 20
MAX_BUTTONS_PER_ROW = 8  # Telegram inline keyboard limit
MAX_LABEL_LENGTH = 100
URL_PATTERN = re.compile(r'^(https?://|tg://)')


# ---- Schemas -----------------------------------------------------------------


class ButtonConfig(BaseModel):
    """Configuration for a single button (built-in or custom URL)."""

    id: str = Field(max_length=100)
    type: Literal['builtin', 'custom']
    style: str = Field(default='primary', max_length=20)
    icon_custom_emoji_id: str = Field(default='', max_length=100)
    enabled: bool = True
    labels: dict[str, str] = Field(default_factory=dict, max_length=10)
    url: str | None = Field(default=None, max_length=2048)
    open_in: Literal['external', 'webapp'] = 'external'


class RowConfig(BaseModel):
    """Configuration for a single row of buttons."""

    id: str = Field(max_length=100)
    max_per_row: int = Field(default=2, ge=1, le=3)
    buttons: list[ButtonConfig] = Field(default_factory=list, max_length=MAX_BUTTONS_PER_ROW)


class MenuConfigResponse(BaseModel):
    """Full merged menu configuration returned to the frontend."""

    rows: list[RowConfig]


class MenuConfigUpdateRequest(BaseModel):
    """Full menu configuration submitted by the frontend."""

    rows: list[RowConfig] = Field(max_length=MAX_ROWS)


# ---- Helpers -----------------------------------------------------------------


async def _get_setting_value(db: AsyncSession, key: str) -> str | None:
    from sqlalchemy import select

    from app.database.models import SystemSetting

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def _upsert_setting(db: AsyncSession, key: str, value: str) -> None:
    """Insert or update a SystemSetting without committing."""
    from sqlalchemy import select

    from app.database.models import SystemSetting

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        setting = SystemSetting(key=key, value=value)
        db.add(setting)


def _build_merged_response(
    layout: dict[str, object],
    button_styles: dict[str, dict],
) -> MenuConfigResponse:
    """Merge layout rows with button_styles into a unified response.

    Built-in buttons get style/emoji/enabled/labels from ``button_styles``.
    Custom URL buttons get all config from layout's ``custom_buttons``.
    """
    custom_buttons: dict[str, dict] = layout.get('custom_buttons', {})

    # Collect row entries sorted numerically (row_1, row_2, ..., row_10, ...)
    row_keys = sorted(
        (k for k in layout if k.startswith('row_')),
        key=lambda k: int(k.split('_', 1)[1]) if k.split('_', 1)[1].isdigit() else 0,
    )

    rows: list[RowConfig] = []
    for row_key in row_keys:
        row_data = layout[row_key]
        if not isinstance(row_data, dict):
            continue

        raw_buttons: list[str] = row_data.get('buttons', [])
        max_per_row: int = row_data.get('max_per_row', 2)
        row_id: str = row_data.get('id', row_key)

        merged_buttons: list[ButtonConfig] = []
        for btn_id in raw_buttons:
            if btn_id in BUILTIN_SECTIONS:
                # Built-in: pull style data from button_styles cache
                style_cfg = button_styles.get(btn_id, {})
                merged_buttons.append(
                    ButtonConfig(
                        id=btn_id,
                        type='builtin',
                        style=style_cfg.get('style', 'primary'),
                        icon_custom_emoji_id=style_cfg.get('icon_custom_emoji_id', ''),
                        enabled=style_cfg.get('enabled', True),
                        labels=style_cfg.get('labels', {}),
                    ),
                )
            elif btn_id.startswith('custom_') and btn_id in custom_buttons:
                # Custom URL button: pull config from layout's custom_buttons
                cb = custom_buttons[btn_id]
                merged_buttons.append(
                    ButtonConfig(
                        id=btn_id,
                        type='custom',
                        style=cb.get('style', 'primary'),
                        icon_custom_emoji_id=cb.get('icon_custom_emoji_id', ''),
                        enabled=cb.get('enabled', True),
                        labels=cb.get('labels', {}),
                        url=cb.get('url'),
                        open_in=cb.get('open_in', 'external'),
                    ),
                )

        rows.append(
            RowConfig(
                id=row_id,
                max_per_row=max_per_row,
                buttons=merged_buttons,
            ),
        )

    return MenuConfigResponse(rows=rows)


def _split_update(
    rows: list[RowConfig],
) -> tuple[dict[str, object], dict[str, dict]]:
    """Split a flat list of RowConfig back into layout_data and button_styles_updates.

    Returns:
        (layout_data, button_styles_updates)
        - layout_data: rows + custom_buttons for ``CABINET_MENU_LAYOUT``
        - button_styles_updates: ``{section: {style, icon_custom_emoji_id, enabled, labels}}``
          for built-in sections only
    """
    layout_data: dict[str, object] = {}
    custom_buttons: dict[str, dict] = {}
    button_styles_updates: dict[str, dict] = {}

    for idx, row in enumerate(rows, start=1):
        row_key = f'row_{idx}'
        button_ids: list[str] = []

        for btn in row.buttons:
            button_ids.append(btn.id)

            if btn.type == 'builtin' and btn.id in BUILTIN_SECTIONS:
                button_styles_updates[btn.id] = {
                    'style': btn.style,
                    'icon_custom_emoji_id': btn.icon_custom_emoji_id,
                    'enabled': btn.enabled,
                    'labels': btn.labels,
                }
            elif btn.type == 'custom' and btn.id.startswith('custom_'):
                custom_buttons[btn.id] = {
                    'id': btn.id,
                    'url': btn.url or '',
                    'style': btn.style,
                    'icon_custom_emoji_id': btn.icon_custom_emoji_id,
                    'enabled': btn.enabled,
                    'labels': btn.labels,
                    'open_in': btn.open_in,
                }

        layout_data[row_key] = {
            'id': row.id or row_key,
            'buttons': button_ids,
            'max_per_row': row.max_per_row,
        }

    layout_data['custom_buttons'] = custom_buttons
    return layout_data, button_styles_updates


def _validate_update_payload(rows: list[RowConfig]) -> None:
    """Validate the full update payload. Raises HTTPException on failure."""
    if len(rows) > MAX_ROWS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Too many rows: {len(rows)}. Maximum allowed: {MAX_ROWS}.',
        )

    # Check for duplicate button IDs across all rows
    seen_ids: set[str] = set()
    for row in rows:
        for btn in row.buttons:
            if btn.id in seen_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Duplicate button ID: "{btn.id}". Each button can only appear once.',
                )
            seen_ids.add(btn.id)

    for row in rows:
        if len(row.buttons) > MAX_BUTTONS_PER_ROW:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Row "{row.id}" has {len(row.buttons)} buttons. Maximum per row: {MAX_BUTTONS_PER_ROW}.',
            )

        for btn in row.buttons:
            # Validate button type consistency
            if btn.type == 'builtin' and btn.id not in BUILTIN_SECTIONS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Unknown built-in section: "{btn.id}".',
                )

            if btn.type == 'custom' and not btn.id.startswith('custom_'):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Custom button id must start with "custom_": "{btn.id}".',
                )

            # Validate URL for custom buttons
            if btn.type == 'custom':
                if not btn.url or not URL_PATTERN.match(btn.url):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f'Custom button "{btn.id}" must have a URL starting with http://, https://, or tg://.',
                    )
                if btn.open_in == 'webapp' and not btn.url.startswith('https://'):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f'Custom button "{btn.id}" with webapp mode requires an https:// URL.',
                    )

            # Validate style
            all_allowed = ALLOWED_STYLE_VALUES | VALID_CUSTOM_BUTTON_STYLES
            if btn.style not in all_allowed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Invalid style "{btn.style}" for button "{btn.id}". '
                    f'Allowed: {", ".join(sorted(all_allowed))}.',
                )

            # Validate labels
            for locale_key, label_val in btn.labels.items():
                if locale_key not in BOT_LOCALES:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f'Invalid locale "{locale_key}" for button "{btn.id}". '
                        f'Allowed: {", ".join(BOT_LOCALES)}.',
                    )
                if not isinstance(label_val, str):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f'Label value for locale "{locale_key}" must be a string.',
                    )
                if len(label_val.strip()) > MAX_LABEL_LENGTH:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f'Label for locale "{locale_key}" on button "{btn.id}" '
                        f'exceeds {MAX_LABEL_LENGTH} characters.',
                    )


# ---- Routes ------------------------------------------------------------------


@router.get('', response_model=MenuConfigResponse)
async def get_menu_layout(
    _admin: User = Depends(require_permission('settings:read')),
):
    """Return merged menu layout config (rows + button styles). Admin only."""
    layout = get_cached_menu_layout()
    button_styles = get_cached_button_styles()
    return _build_merged_response(layout, button_styles)


@router.put('', response_model=MenuConfigResponse)
async def update_menu_layout(
    payload: MenuConfigUpdateRequest,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Save full menu layout config. Splits into layout + button styles. Admin only."""
    _validate_update_payload(payload.rows)

    layout_data, button_styles_updates = _split_update(payload.rows)

    # Save layout to CABINET_MENU_LAYOUT (without committing)
    await _upsert_setting(db, MENU_LAYOUT_KEY, json.dumps(layout_data))

    # Merge button styles updates with existing styles (don't overwrite sections not in request)
    if button_styles_updates:
        raw = await _get_setting_value(db, BUTTON_STYLES_KEY)
        current_styles: dict[str, dict] = {}
        if raw:
            try:
                current_styles = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                current_styles = {}

        for section, updates in button_styles_updates.items():
            current_styles[section] = updates

        await _upsert_setting(db, BUTTON_STYLES_KEY, json.dumps(current_styles))

    # Single atomic commit for both settings
    await db.commit()

    # Refresh caches after commit
    await load_button_styles_cache()
    await load_menu_layout_cache()

    logger.info(
        'Admin updated menu layout',
        telegram_id=admin.telegram_id,
        rows_count=len(payload.rows),
        custom_buttons_count=len(layout_data.get('custom_buttons', {})),
    )

    # Return merged response from fresh caches
    layout = get_cached_menu_layout()
    button_styles = get_cached_button_styles()
    return _build_merged_response(layout, button_styles)


@router.post('/reset', response_model=MenuConfigResponse)
async def reset_menu_layout(
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reset menu layout AND button styles to defaults. Admin only."""
    await _upsert_setting(db, MENU_LAYOUT_KEY, json.dumps(DEFAULT_MENU_LAYOUT))
    await _upsert_setting(db, BUTTON_STYLES_KEY, json.dumps(DEFAULT_BUTTON_STYLES))

    # Single atomic commit for both settings
    await db.commit()

    # Refresh caches after commit
    await load_button_styles_cache()
    await load_menu_layout_cache()

    logger.info('Admin reset menu layout and button styles to defaults', telegram_id=admin.telegram_id)

    layout = get_cached_menu_layout()
    button_styles = get_cached_button_styles()
    return _build_merged_response(layout, button_styles)
