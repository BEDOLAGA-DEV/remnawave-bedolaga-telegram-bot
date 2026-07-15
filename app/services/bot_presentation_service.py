"""Semantic, Russian-only presentation overrides for Telegram bot output.

Emoji overrides are keyed by a localization token (``LOCALE_KEY#occurrence``),
not by a Unicode glyph. ``Texts`` embeds private runtime markers only for Russian
localized values; the outgoing request middleware converts those markers to
Telegram custom emoji or strips them back to Unicode fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from string import Formatter
from typing import Any

import structlog
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from sqlalchemy import select


logger = structlog.get_logger(__name__)

BOT_PRESENTATION_KEY = 'BOT_PRESENTATION_CONFIG'
BOT_PRESENTATION_VERSION = 2
MAX_EMOJI_OVERRIDES = 2500
MAX_TEXT_OVERRIDES = 2500
MAX_TEXT_LENGTH = 8192
CACHE_REFRESH_INTERVAL_SECONDS = 30.0
_CUSTOM_EMOJI_ID_RE = re.compile(r'^\d{1,100}$')
_TOKEN_RE = re.compile(r'^[A-Z][A-Z0-9_]*#(?:0|[1-9]\d*):.{1,32}$', re.DOTALL)
_MARKER_RE = re.compile(r'\ue000(\d{1,100})\ue001(.*?)\ue002', re.DOTALL)
_HTML_TAG_RE = re.compile(r'</?[A-Za-z][^<>]*?>')

# Unicode emoji sequences: keycaps, flags, optional variation selector/skin
# modifier, and ZWJ compositions. Longest alternatives must come first.
_EMOJI_BASE = (
    r'[\U0001F300-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\u00A9\u00AE\u2122]'
)
_EMOJI_ELEMENT = rf'{_EMOJI_BASE}[\uFE0E\uFE0F]?[\U0001F3FB-\U0001F3FF]?'
_EMOJI_RE = re.compile(
    rf'(?:[0-9#*][\uFE0E\uFE0F]?\u20E3|'
    rf'[\U0001F1E6-\U0001F1FF]{{2}}|'
    rf'{_EMOJI_ELEMENT}(?:\u200D{_EMOJI_ELEMENT})*)'
)


@dataclass(slots=True)
class BotPresentationConfig:
    version: int = BOT_PRESENTATION_VERSION
    emoji_overrides: dict[str, str] = field(default_factory=dict)
    text_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Any) -> BotPresentationConfig:
        if not isinstance(raw, dict):
            return cls()
        emoji_raw = raw.get('emoji_overrides', {})
        text_raw = raw.get('text_overrides', {})
        emoji = (
            {
                str(token): str(custom_id).strip()
                for token, custom_id in emoji_raw.items()
                if isinstance(token, str)
                and _TOKEN_RE.fullmatch(token)
                and isinstance(custom_id, str)
                and _CUSTOM_EMOJI_ID_RE.fullmatch(custom_id.strip())
            }
            if isinstance(emoji_raw, dict)
            else {}
        )
        texts = (
            {
                str(key): str(value)
                for key, value in text_raw.items()
                if isinstance(key, str) and key and isinstance(value, str) and value.strip()
            }
            if isinstance(text_raw, dict)
            else {}
        )
        return cls(
            version=BOT_PRESENTATION_VERSION,
            emoji_overrides=dict(list(emoji.items())[:MAX_EMOJI_OVERRIDES]),
            text_overrides=dict(list(texts.items())[:MAX_TEXT_OVERRIDES]),
        )

    def to_raw(self) -> dict[str, Any]:
        return {
            'version': BOT_PRESENTATION_VERSION,
            'emoji_overrides': dict(self.emoji_overrides),
            'text_overrides': dict(self.text_overrides),
        }


_config = BotPresentationConfig()
_cache_loaded_at = 0.0
_refresh_lock = asyncio.Lock()


def clear_bot_presentation_cache() -> None:
    set_bot_presentation_cache(BotPresentationConfig())


def set_bot_presentation_cache(config: BotPresentationConfig) -> None:
    global _config, _cache_loaded_at
    _config = BotPresentationConfig.from_raw(config.to_raw())
    _cache_loaded_at = time.monotonic()


def get_bot_presentation_config() -> BotPresentationConfig:
    return BotPresentationConfig.from_raw(_config.to_raw())


async def load_bot_presentation_cache() -> BotPresentationConfig:
    """Load and revalidate persisted overrides against the current upstream."""
    try:
        from app.database.database import AsyncSessionLocal
        from app.database.models import SystemSetting
        from app.services.bot_presentation_catalog import validate_config_against_catalog

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SystemSetting.value).where(SystemSetting.key == BOT_PRESENTATION_KEY))
            raw_value = result.scalar_one_or_none()
        raw = json.loads(raw_value) if raw_value else {}
        config = BotPresentationConfig.from_raw(raw)
        validate_config_against_catalog(config)
    except Exception as error:  # pragma: no cover - defensive startup path
        logger.warning('Failed to load valid bot presentation config; using upstream defaults', error=str(error))
        config = BotPresentationConfig()
    set_bot_presentation_cache(config)
    return get_bot_presentation_config()


async def maybe_refresh_bot_presentation_cache() -> None:
    """Refresh from DB periodically so multiple workers converge after edits."""
    if time.monotonic() - _cache_loaded_at < CACHE_REFRESH_INTERVAL_SECONDS:
        return
    async with _refresh_lock:
        if time.monotonic() - _cache_loaded_at < CACHE_REFRESH_INTERVAL_SECONDS:
            return
        await load_bot_presentation_cache()


def get_text_override(language: str | None, key: str) -> str | None:
    normalized = (language or '').split('-', 1)[0].lower()
    if normalized != 'ru':
        return None
    return _config.text_overrides.get(key)


def extract_emoji(value: str) -> list[str]:
    """Return complete Unicode emoji grapheme-like sequences in source order."""
    return [match.group(0) for match in _EMOJI_RE.finditer(value)]


def decorate_localized_text(language: str | None, key: str, value: Any) -> Any:
    """Embed semantic markers for configured emoji in one Russian locale key."""
    normalized = (language or '').split('-', 1)[0].lower()
    if normalized != 'ru' or not isinstance(value, str):
        return value

    matches = list(_EMOJI_RE.finditer(value))
    replacements: list[tuple[int, int, str]] = []
    for index, match in enumerate(matches):
        custom_id = _config.emoji_overrides.get(f'{key}#{index}:{match.group(0)}')
        if custom_id:
            glyph = match.group(0)
            replacements.append((match.start(), match.end(), f'\ue000{custom_id}\ue001{glyph}\ue002'))
    if not replacements:
        return value
    rendered = value
    for start, end, replacement in reversed(replacements):
        rendered = rendered[:start] + replacement + rendered[end:]
    return rendered


def strip_presentation_markers(value: str | None) -> str | None:
    if not value:
        return value
    return _MARKER_RE.sub(lambda match: match.group(2), value)


def apply_html_emoji(value: str | None) -> str | None:
    """Convert semantic markers to Telegram HTML custom-emoji tags."""
    if not value:
        return value
    return _MARKER_RE.sub(
        lambda match: f'<tg-emoji emoji-id="{match.group(1)}">{match.group(2)}</tg-emoji>',
        value,
    )


def _html_structure(value: str) -> list[str]:
    tags = _HTML_TAG_RE.findall(value)
    remainder = _HTML_TAG_RE.sub('', value)
    if '<' in remainder:
        raise ValueError('invalid or unbalanced HTML')
    return tags


def _placeholder_signature(value: str) -> Counter[tuple[str, str | None, str | None]]:
    signature: Counter[tuple[str, str | None, str | None]] = Counter()
    for _, field_name, format_spec, conversion in Formatter().parse(value):
        if field_name:
            signature[(field_name, format_spec, conversion)] += 1
    return signature


def validate_text_override(default: str, override: str) -> None:
    if any(marker in override for marker in ('\ue000', '\ue001', '\ue002')):
        raise ValueError('reserved presentation markers are not allowed')
    if len(override) > MAX_TEXT_LENGTH:
        raise ValueError(f'text exceeds {MAX_TEXT_LENGTH} characters')
    expected = _placeholder_signature(default)
    actual = _placeholder_signature(override)
    if expected != actual:
        fields = ', '.join(sorted({item[0] for item in expected} | {item[0] for item in actual}))
        raise ValueError(
            f'placeholders, conversions and format specifiers must match the upstream text: {fields}'
        )
    if _html_structure(default) != _html_structure(override):
        raise ValueError('HTML structure must match the upstream text')
    if extract_emoji(default) != extract_emoji(override):
        raise ValueError('Unicode emoji sequence must match the upstream text')


def _render_button_text(text: str, *, custom: bool, existing_icon: str | None) -> tuple[str, str | None]:
    match = _MARKER_RE.search(text)
    if not match:
        return strip_presentation_markers(text) or text, existing_icon
    custom_id, glyph = match.group(1), match.group(2)
    if existing_icon:
        return strip_presentation_markers(text) or text, existing_icon
    if custom:
        without_selected = (text[: match.start()] + text[match.end() :]).strip()
        rendered = strip_presentation_markers(without_selected) or glyph
        return rendered, custom_id
    return strip_presentation_markers(text) or text, None


def apply_button_presentation(markup: Any, *, custom: bool = True) -> Any:
    """Render semantic markers in inline and reply keyboard labels only."""
    if not isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
        return markup
    rows = markup.inline_keyboard if isinstance(markup, InlineKeyboardMarkup) else markup.keyboard
    if not any(_MARKER_RE.search(button.text) for row in rows for button in row):
        return markup

    rendered = markup.model_copy(deep=True)
    rendered_rows = (
        rendered.inline_keyboard if isinstance(rendered, InlineKeyboardMarkup) else rendered.keyboard
    )
    for row in rendered_rows:
        for button in row:
            existing = getattr(button, 'icon_custom_emoji_id', None)
            button.text, icon = _render_button_text(button.text, custom=custom, existing_icon=existing)
            if hasattr(button, 'icon_custom_emoji_id'):
                button.icon_custom_emoji_id = icon
    return rendered
