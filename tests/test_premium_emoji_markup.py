"""Тесты замены ведущих эмодзи inline-кнопок на icon_custom_emoji_id."""

import json

import pytest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.utils import premium_emoji
from app.utils.premium_emoji import apply_premium_emoji_to_markup
from app.utils.premium_emoji_middleware import PremiumEmojiRequestMiddleware


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


def _capture():
    captured = {}

    async def fake_make_request(bot, method):
        captured['method'] = method
        return 'ok'

    return captured, fake_make_request


@pytest.mark.asyncio
async def test_middleware_transforms_send_message(emoji_map):
    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()
    method = SendMessage(chat_id=1, text='hi', reply_markup=_kb('\U0001f48e Купить'))

    result = await mw(fake_make_request, None, method)

    assert result == 'ok'
    btn = captured['method'].reply_markup.inline_keyboard[0][0]
    assert btn.text == 'Купить'
    assert btn.icon_custom_emoji_id == emoji_map['\U0001f48e']


@pytest.mark.asyncio
async def test_middleware_passthrough_reply_keyboard(emoji_map):
    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='\U0001f48e Купить')]])
    method = SendMessage(chat_id=1, text='hi', reply_markup=kb)

    await mw(fake_make_request, None, method)

    assert captured['method'] is method


@pytest.mark.asyncio
async def test_middleware_passthrough_no_markup(emoji_map):
    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()
    method = SendMessage(chat_id=1, text='hi')

    await mw(fake_make_request, None, method)

    assert captured['method'] is method


@pytest.mark.asyncio
async def test_middleware_error_sends_original(emoji_map, monkeypatch):
    import app.utils.premium_emoji_middleware as mw_module

    mw = PremiumEmojiRequestMiddleware()
    captured, fake_make_request = _capture()

    def boom(markup):
        raise RuntimeError('boom')

    monkeypatch.setattr(mw_module, 'apply_premium_emoji_to_markup', boom)
    method = SendMessage(chat_id=1, text='hi', reply_markup=_kb('\U0001f48e Купить'))

    result = await mw(fake_make_request, None, method)

    assert result == 'ok'
    assert captured['method'] is method


def test_create_bot_registers_premium_emoji_middleware(monkeypatch):
    from app import bot_factory

    monkeypatch.setattr(type(bot_factory.settings), 'get_proxy_url', lambda self: None)
    monkeypatch.setattr(type(bot_factory.settings), 'get_telegram_api_url', lambda self: None)

    bot = bot_factory.create_bot(token='42:TEST')

    assert any(
        isinstance(m, PremiumEmojiRequestMiddleware)
        for m in bot.session.middleware
    )


# --- VS16 (U+FE0F) normalization: bare/вариантные формы эмодзи ---

VS16 = '️'
STAR_BARE = '⭐'  # ⭐
STAR_VS16 = STAR_BARE + VS16  # ⭐️
CALENDAR_BARE = '\U0001f5d3'  # 🗓
CALENDAR_VS16 = CALENDAR_BARE + VS16  # 🗓️
BOLT_BARE = '⚡'  # ⚡
BOLT_VS16 = BOLT_BARE + VS16  # ⚡️


def _use_mapping(monkeypatch, tmp_path, mapping):
    path = tmp_path / 'premium_emoji.json'
    path.write_text(json.dumps({'emojis': mapping}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(premium_emoji, '_EMOJI_MAP_PATH', path)
    premium_emoji.reload_premium_emoji()


def test_vs16_text_matches_bare_mapping(monkeypatch, tmp_path):
    _use_mapping(monkeypatch, tmp_path, {CALENDAR_BARE: '6000000000000000001'})
    try:
        markup = _kb(f'{CALENDAR_VS16} План')
        result = apply_premium_emoji_to_markup(markup)
        btn = result.inline_keyboard[0][0]
        assert btn.text == 'План'  # без остаточного U+FE0F
        assert btn.icon_custom_emoji_id == '6000000000000000001'
    finally:
        monkeypatch.undo()
        premium_emoji.reload_premium_emoji()


def test_bare_text_matches_vs16_mapping(monkeypatch, tmp_path):
    _use_mapping(monkeypatch, tmp_path, {STAR_VS16: '6000000000000000002'})
    try:
        markup = _kb(f'{STAR_BARE} Звёзды')
        result = apply_premium_emoji_to_markup(markup)
        btn = result.inline_keyboard[0][0]
        assert btn.text == 'Звёзды'
        assert btn.icon_custom_emoji_id == '6000000000000000002'
    finally:
        monkeypatch.undo()
        premium_emoji.reload_premium_emoji()


def test_explicit_variant_ids_not_overwritten(monkeypatch, tmp_path):
    _use_mapping(monkeypatch, tmp_path, {
        BOLT_BARE: '6000000000000000003',
        BOLT_VS16: '6000000000000000004',
    })
    try:
        result = apply_premium_emoji_to_markup(_kb(f'{BOLT_BARE} A'))
        assert result.inline_keyboard[0][0].icon_custom_emoji_id == '6000000000000000003'
        result = apply_premium_emoji_to_markup(_kb(f'{BOLT_VS16} B'))
        assert result.inline_keyboard[0][0].icon_custom_emoji_id == '6000000000000000004'
    finally:
        monkeypatch.undo()
        premium_emoji.reload_premium_emoji()


def test_text_replacement_vs16_normalized(monkeypatch, tmp_path):
    _use_mapping(monkeypatch, tmp_path, {STAR_VS16: '6000000000000000005'})
    try:
        out = premium_emoji.apply_premium_emoji(f'баланс {STAR_BARE}')
        assert '<tg-emoji emoji-id="6000000000000000005">' in out
    finally:
        monkeypatch.undo()
        premium_emoji.reload_premium_emoji()
