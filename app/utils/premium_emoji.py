"""
Замена обычных эмодзи на премиум (кастомные) эмодзи Telegram.

Маппинг хранится в data/premium_emoji.json. Ключи — символы эмодзи,
значения — document_id кастомного эмодзи. Пустая строка означает «не задано»,
такие эмодзи остаются без изменений.

Формат замены (требует parse_mode=HTML):
    <tg-emoji emoji-id="DOCUMENT_ID">EMOJI</tg-emoji>
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    pass

_logger = structlog.get_logger(__name__)

_EMOJI_MAP_PATH = Path(__file__).resolve().parents[2] / 'data' / 'premium_emoji.json'

# Кеш: emoji_char -> "<tg-emoji emoji-id="ID">CHAR</tg-emoji>" или None если ID не задан
_replacement_cache: dict[str, str] | None = None


def _load_replacements() -> dict[str, str]:
    """Загружает маппинг из JSON и строит готовые замены."""
    global _replacement_cache
    if _replacement_cache is not None:
        return _replacement_cache

    try:
        with _EMOJI_MAP_PATH.open(encoding='utf-8') as f:
            data = json.load(f)
        raw = data.get('emojis', {})
    except FileNotFoundError:
        _logger.warning('premium_emoji.json not found, premium emoji replacement is disabled')
        _replacement_cache = {}
        return _replacement_cache
    except Exception as exc:
        _logger.warning('Failed to load premium_emoji.json', error=exc)
        _replacement_cache = {}
        return _replacement_cache

    mapping: dict[str, str] = {}
    for emoji_char, doc_id in raw.items():
        if doc_id and isinstance(doc_id, str) and doc_id.strip():
            mapping[emoji_char] = f'<tg-emoji emoji-id="{doc_id.strip()}">{emoji_char}</tg-emoji>'

    _replacement_cache = mapping
    _logger.debug('Loaded premium emoji replacements', count=len(mapping))
    return _replacement_cache


def _build_pattern(mapping: dict[str, str]) -> re.Pattern[str] | None:
    """Строит regex для поиска всех настроенных эмодзи сразу."""
    if not mapping:
        return None
    # Сортируем по убыванию длины чтобы многосимвольные эмодзи (напр. 🧑‍🤝‍🧑) матчились раньше
    sorted_emojis = sorted(mapping.keys(), key=len, reverse=True)
    pattern = '|'.join(re.escape(e) for e in sorted_emojis)
    return re.compile(pattern)


_pattern_cache: re.Pattern[str] | None = None
_pattern_map_id: int = -1  # id() словаря, чтобы пересобрать паттерн если маппинг изменился


def apply_premium_emoji(text: str) -> str:
    """Заменяет обычные эмодзи на кастомные (премиум) в тексте сообщения.

    Работает только с эмодзи, у которых задан document_id в data/premium_emoji.json.
    Пропускает эмодзи, которые уже обёрнуты в <tg-emoji>...</tg-emoji>.
    Безопасен для многократного вызова (идемпотентен).

    Args:
        text: Исходный HTML-текст сообщения.

    Returns:
        Текст с заменёнными эмодзи.
    """
    if not text:
        return text

    global _pattern_cache, _pattern_map_id

    mapping = _load_replacements()
    if not mapping:
        return text

    # Пересобираем паттерн если словарь изменился
    if _pattern_cache is None or id(mapping) != _pattern_map_id:
        _pattern_cache = _build_pattern(mapping)
        _pattern_map_id = id(mapping)

    if _pattern_cache is None:
        return text

    # Используем split по <tg-emoji>...</tg-emoji> блокам чтобы не трогать
    # эмодзи которые уже являются кастомными
    _TG_EMOJI_TAG_RE = re.compile(r'(<tg-emoji[^>]*>.*?</tg-emoji>)', re.DOTALL)

    parts = _TG_EMOJI_TAG_RE.split(text)
    result_parts: list[str] = []
    for part in parts:
        if part.startswith('<tg-emoji'):
            # Уже кастомный эмодзи — не трогаем
            result_parts.append(part)
        else:
            result_parts.append(_pattern_cache.sub(lambda m: mapping[m.group(0)], part))

    return ''.join(result_parts)


def reload_premium_emoji() -> None:
    """Сбрасывает кеш маппинга (для горячей перезагрузки без рестарта)."""
    global _replacement_cache, _pattern_cache, _pattern_map_id
    _replacement_cache = None
    _pattern_cache = None
    _pattern_map_id = -1
