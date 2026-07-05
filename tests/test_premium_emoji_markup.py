"""Тесты замены ведущих эмодзи inline-кнопок на icon_custom_emoji_id."""

import json

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils import premium_emoji
from app.utils.premium_emoji import apply_premium_emoji_to_markup


HEART = '❤️'
HEART_FIRE = '❤️‍\U0001f525'  # ❤️‍🔥 — multi-codepoint, HEART is its prefix


@pytest.fixture()
def emoji_map(monkeypatch, tmp_path):
    mapping = {
        '\U0001f48e': '5000000000000000001',  # 💎
        '◀️': '5000000000000000002',
        HEART: '5000000000000000003',
        HEART_FIRE: '5000000000000000004',
    }
    path = tmp_path / 'premium_emoji.json'
    path.write_text(json.dumps({'emojis': mapping}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(premium_emoji, '_EMOJI_MAP_PATH', path)
    premium_emoji.reload_premium_emoji()
    yield mapping
    monkeypatch.undo()
    premium_emoji.reload_premium_emoji()


def _kb(*texts: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=f'cb{i}') for i, t in enumerate(texts)],
        ],
    )


def test_leading_emoji_stripped_and_icon_set(emoji_map):
    markup = _kb('\U0001f48e Купить')
    result = apply_premium_emoji_to_markup(markup)
    btn = result.inline_keyboard[0][0]
    assert btn.text == 'Купить'
    assert btn.icon_custom_emoji_id == emoji_map['\U0001f48e']
    assert btn.callback_data == 'cb0'


def test_original_markup_not_mutated(emoji_map):
    markup = _kb('\U0001f48e Купить')
    apply_premium_emoji_to_markup(markup)
    original_btn = markup.inline_keyboard[0][0]
    assert original_btn.text == '\U0001f48e Купить'
    assert original_btn.icon_custom_emoji_id is None


def test_emoji_only_button_untouched(emoji_map):
    markup = _kb('◀️')
    assert apply_premium_emoji_to_markup(markup) is markup


def test_mid_text_emoji_untouched(emoji_map):
    markup = _kb('Купить \U0001f48e')
    assert apply_premium_emoji_to_markup(markup) is markup


def test_multicodepoint_longest_match(emoji_map):
    markup = _kb(f'{HEART_FIRE} Огонь')
    result = apply_premium_emoji_to_markup(markup)
    btn = result.inline_keyboard[0][0]
    assert btn.text == 'Огонь'
    assert btn.icon_custom_emoji_id == emoji_map[HEART_FIRE]


def test_already_set_icon_untouched(emoji_map):
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='\U0001f48e Купить',
                    callback_data='cb',
                    icon_custom_emoji_id='123',
                ),
            ],
        ],
    )
    assert apply_premium_emoji_to_markup(markup) is markup


def test_unmapped_emoji_untouched(emoji_map):
    markup = _kb('\U0001f680 Старт')  # 🚀 отсутствует в маппинге
    assert apply_premium_emoji_to_markup(markup) is markup


def test_none_markup(emoji_map):
    assert apply_premium_emoji_to_markup(None) is None


def test_mixed_row_only_changed_buttons_copied(emoji_map):
    markup = _kb('\U0001f48e Купить', 'Просто текст')
    result = apply_premium_emoji_to_markup(markup)
    assert result is not markup
    assert result.inline_keyboard[0][0].icon_custom_emoji_id == emoji_map['\U0001f48e']
    # Неизменённая кнопка переиспользуется как есть
    assert result.inline_keyboard[0][1] is markup.inline_keyboard[0][1]


def test_empty_mapping_disables(monkeypatch, tmp_path):
    path = tmp_path / 'premium_emoji.json'
    path.write_text(json.dumps({'emojis': {}}), encoding='utf-8')
    monkeypatch.setattr(premium_emoji, '_EMOJI_MAP_PATH', path)
    premium_emoji.reload_premium_emoji()
    try:
        markup = _kb('\U0001f48e Купить')
        assert apply_premium_emoji_to_markup(markup) is markup
    finally:
        monkeypatch.undo()
        premium_emoji.reload_premium_emoji()


def test_text_replacement_still_works_after_refactor(emoji_map):
    out = premium_emoji.apply_premium_emoji('привет \U0001f48e')
    diamond_id = emoji_map['\U0001f48e']
    expected = f'<tg-emoji emoji-id="{diamond_id}">\U0001f48e</tg-emoji>'
    assert expected in out
