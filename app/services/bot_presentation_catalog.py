"""Semantic catalog for editable Russian bot presentation tokens.

Only localization keys actually referenced through ``Texts`` are exposed. Emoji
entries are scoped to ``RU_LOCALE_KEY#occurrence``; arbitrary Python literals,
logs, Cabinet strings, docstrings, and non-Russian locales are intentionally not
part of the operator catalog.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.services.bot_presentation_service import (
    MAX_EMOJI_OVERRIDES,
    MAX_TEXT_OVERRIDES,
    BotPresentationConfig,
    extract_emoji,
    validate_text_override,
)


_APP_ROOT = Path(__file__).resolve().parents[1]
_RU_LOCALE_PATH = _APP_ROOT / 'localization' / 'locales' / 'ru.json'
_CUSTOM_ID_RE = re.compile(r'^\d{1,100}$')
_MAX_USAGES_PER_ITEM = 20
_NON_OVERLAY_KEYS = {
    'RULES_TEXT',
    'SUPPORT_INFO',
    'TRAFFIC_5GB',
    'TRAFFIC_10GB',
    'TRAFFIC_25GB',
    'TRAFFIC_50GB',
    'TRAFFIC_100GB',
    'TRAFFIC_250GB',
    'TRAFFIC_UNLIMITED',
}
_EXCLUDED_USAGE_PREFIXES = (
    'app/cabinet/',
    'app/database/',
    'app/webapi/',
    'app/webserver/',
)
_EXCLUDED_USAGE_FILES = {
    'app/bot.py',
    'app/config.py',
    'app/logging_config.py',
    'app/logging_handler.py',
}


@dataclass(slots=True)
class EmojiCatalogItem:
    token: str
    localization_key: str
    occurrence: int
    glyph: str
    usage_count: int = 0
    usages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TextCatalogItem:
    key: str
    default: str
    usage_count: int = 0
    usages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BotPresentationCatalog:
    emoji: dict[str, EmojiCatalogItem]
    texts: dict[str, TextCatalogItem]


def _literal_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _add_usage(usages: dict[str, set[str]], key: str | None, location: str, locale_keys: set[str]) -> None:
    if not key or key not in locale_keys or key in _NON_OVERLAY_KEYS:
        return
    usages.setdefault(key, set()).add(location)


def _looks_like_texts_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {'texts', 'text', 'locale_texts'}
    if isinstance(node, ast.Attribute):
        return node.attr in {'texts', '_texts'}
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id in {'Texts', 'get_texts'}
        if isinstance(node.func, ast.Attribute):
            return node.func.attr == 'get_texts'
    return False


def _discover_text_usages(locale_keys: set[str]) -> dict[str, set[str]]:
    usages: dict[str, set[str]] = {}
    for path in _APP_ROOT.rglob('*.py'):
        relative = path.relative_to(_APP_ROOT.parent)
        relative_text = relative.as_posix()
        if relative_text in _EXCLUDED_USAGE_FILES or relative_text.startswith(_EXCLUDED_USAGE_PREFIXES):
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            location = f'{relative}:{getattr(node, "lineno", 0)}'
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {'t', 'get'}
                and _looks_like_texts_receiver(node.func.value)
            ):
                key = _literal_string(node.args[0]) if node.args else None
                _add_usage(usages, key, location, locale_keys)
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in locale_keys
                and _looks_like_texts_receiver(node.value)
            ):
                _add_usage(usages, node.attr, location, locale_keys)
            elif isinstance(node, ast.Subscript) and _looks_like_texts_receiver(node.value):
                key = _literal_string(node.slice)
                _add_usage(usages, key, location, locale_keys)
    return usages


@lru_cache(maxsize=1)
def build_bot_presentation_catalog() -> BotPresentationCatalog:
    locale_data = json.loads(_RU_LOCALE_PATH.read_text(encoding='utf-8'))
    locale_values = {
        key: value
        for key, value in locale_data.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    usages = _discover_text_usages(set(locale_values))

    texts: dict[str, TextCatalogItem] = {}
    for key, locations in usages.items():
        ordered_locations = sorted(locations)
        texts[key] = TextCatalogItem(
            key=key,
            default=locale_values[key],
            usage_count=len(ordered_locations),
            usages=ordered_locations[:_MAX_USAGES_PER_ITEM],
        )
    emoji: dict[str, EmojiCatalogItem] = {}
    for key, item in texts.items():
        glyphs = extract_emoji(item.default)
        for occurrence, glyph in enumerate(glyphs):
            token = f'{key}#{occurrence}:{glyph}'
            emoji[token] = EmojiCatalogItem(
                token=token,
                localization_key=key,
                occurrence=occurrence,
                glyph=glyph,
                usage_count=item.usage_count,
                usages=list(item.usages),
            )

    return BotPresentationCatalog(
        emoji=dict(
            sorted(
                emoji.items(),
                key=lambda pair: (-pair[1].usage_count, pair[1].localization_key, pair[1].occurrence),
            )
        ),
        texts=dict(sorted(texts.items())),
    )


def validate_config_against_catalog(config: BotPresentationConfig) -> None:
    catalog = build_bot_presentation_catalog()
    if len(config.emoji_overrides) > MAX_EMOJI_OVERRIDES:
        raise ValueError(f'too many emoji overrides; maximum is {MAX_EMOJI_OVERRIDES}')
    if len(config.text_overrides) > MAX_TEXT_OVERRIDES:
        raise ValueError(f'too many text overrides; maximum is {MAX_TEXT_OVERRIDES}')

    for token, custom_id in config.emoji_overrides.items():
        if token not in catalog.emoji:
            raise ValueError(f'unknown semantic emoji token: {token!r}')
        if not _CUSTOM_ID_RE.fullmatch(custom_id.strip()):
            raise ValueError(f'invalid custom emoji id for {token!r}')

    for key, override in config.text_overrides.items():
        item = catalog.texts.get(key)
        if item is None:
            raise ValueError(f'unknown or non-output text key: {key}')
        validate_text_override(item.default, override)
