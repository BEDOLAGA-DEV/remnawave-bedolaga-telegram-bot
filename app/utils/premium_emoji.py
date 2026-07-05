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

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_logger = structlog.get_logger(__name__)

_EMOJI_MAP_PATH = Path(__file__).resolve().parents[2] / 'data' / 'premium_emoji.json'

# Кеш: emoji_char -> "<tg-emoji emoji-id="ID">CHAR</tg-emoji>" или None если ID не задан
_replacement_cache: dict[str, str] | None = None

# Кеш сырого маппинга: emoji_char -> document_id
_raw_map_cache: dict[str, str] | None = None


def _load_raw_map() -> dict[str, str]:
    """Загружает сырой маппинг emoji -> document_id из JSON."""
    global _raw_map_cache
    if _raw_map_cache is not None:
        return _raw_map_cache

    try:
        with _EMOJI_MAP_PATH.open(encoding='utf-8') as f:
            data = json.load(f)
        raw = data.get('emojis', {})
    except FileNotFoundError:
        _logger.warning('premium_emoji.json not found, premium emoji replacement is disabled')
        _raw_map_cache = {}
        return _raw_map_cache
    except Exception as exc:
        _logger.warning('Failed to load premium_emoji.json', error=exc)
        _raw_map_cache = {}
        return _raw_map_cache

    _raw_map_cache = {
        emoji_char: doc_id.strip()
        for emoji_char, doc_id in raw.items()
        if doc_id and isinstance(doc_id, str) and doc_id.strip()
    }
    return _raw_map_cache


def _load_replacements() -> dict[str, str]:
    """Строит готовые <tg-emoji> замены из сырого маппинга."""
    global _replacement_cache
    if _replacement_cache is not None:
        return _replacement_cache

    raw = _load_raw_map()
    _replacement_cache = {
        emoji_char: f'<tg-emoji emoji-id="{doc_id}">{emoji_char}</tg-emoji>'
        for emoji_char, doc_id in raw.items()
    }
    if _replacement_cache:
        _logger.debug('Loaded premium emoji replacements', count=len(_replacement_cache))
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


_leading_pattern_cache: re.Pattern[str] | None = None
_leading_pattern_map_id: int = -1


def _get_leading_pattern() -> re.Pattern[str] | None:
    """Regex, матчащий настроенный эмодзи в начале строки (длинные раньше)."""
    global _leading_pattern_cache, _leading_pattern_map_id
    raw = _load_raw_map()
    if not raw:
        return None
    if _leading_pattern_cache is None or id(raw) != _leading_pattern_map_id:
        sorted_emojis = sorted(raw.keys(), key=len, reverse=True)
        _leading_pattern_cache = re.compile(
            '^(?:' + '|'.join(re.escape(e) for e in sorted_emojis) + ')',
        )
        _leading_pattern_map_id = id(raw)
    return _leading_pattern_cache


def _convert_button(
    button: InlineKeyboardButton,
    raw_map: dict[str, str],
    pattern: re.Pattern[str],
) -> InlineKeyboardButton:
    """Кнопка с icon_custom_emoji_id, либо исходная если замена не нужна."""
    if button.icon_custom_emoji_id:
        return button
    text = button.text or ''
    match = pattern.match(text)
    if not match:
        return button
    emoji = match.group(0)
    stripped = text[len(emoji):].lstrip()
    if not stripped:
        # Кнопка из одного эмодзи: Telegram требует непустой text
        return button
    return button.model_copy(
        update={'text': stripped, 'icon_custom_emoji_id': raw_map[emoji]},
    )


def apply_premium_emoji_to_markup(
    markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    """Заменяет ведущие эмодзи inline-кнопок на icon_custom_emoji_id.

    Возвращает новый InlineKeyboardMarkup, если хоть одна кнопка изменилась,
    иначе исходный объект (identity сохраняется). Исходная разметка и её
    кнопки не мутируются — клавиатуры могут переиспользоваться между
    отправками.
    """
    if markup is None or not isinstance(markup, InlineKeyboardMarkup):
        return markup

    raw_map = _load_raw_map()
    if not raw_map:
        return markup
    pattern = _get_leading_pattern()
    if pattern is None:
        return markup

    changed = False
    new_rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            new_button = _convert_button(button, raw_map, pattern)
            if new_button is not button:
                changed = True
            new_row.append(new_button)
        new_rows.append(new_row)

    if not changed:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


def reload_premium_emoji() -> None:
    """Сбрасывает кеши маппинга (для горячей перезагрузки без рестарта)."""
    global _replacement_cache, _pattern_cache, _pattern_map_id
    global _raw_map_cache, _leading_pattern_cache, _leading_pattern_map_id
    _replacement_cache = None
    _pattern_cache = None
    _pattern_map_id = -1
    _raw_map_cache = None
    _leading_pattern_cache = None
    _leading_pattern_map_id = -1
