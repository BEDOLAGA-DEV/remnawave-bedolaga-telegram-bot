import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.localization.texts import Texts
from app.services import bot_presentation_service as presentation_service
from app.services.bot_presentation_service import (
    BotPresentationConfig,
    apply_button_presentation,
    apply_html_emoji,
    clear_bot_presentation_cache,
    decorate_localized_text,
    extract_emoji,
    get_text_override,
    set_bot_presentation_cache,
    strip_presentation_markers,
    validate_text_override,
)


CUSTOM_ID = '5368324170671202286'
TOKEN = 'PROMO_GROUP_DISCOUNT_TRAFFIC#0:📊'


def teardown_function() -> None:
    clear_bot_presentation_cache()


async def test_stale_cache_refresh_is_coalesced_for_concurrent_requests(monkeypatch) -> None:
    monkeypatch.setattr(presentation_service, '_cache_loaded_at', 0.0)

    async def refresh() -> BotPresentationConfig:
        await asyncio.sleep(0)
        config = BotPresentationConfig()
        set_bot_presentation_cache(config)
        return config

    loader = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(presentation_service, 'load_bot_presentation_cache', loader)

    await asyncio.gather(
        presentation_service.maybe_refresh_bot_presentation_cache(),
        presentation_service.maybe_refresh_bot_presentation_cache(),
    )
    loader.assert_awaited_once()


def test_russian_text_override_preserves_other_languages() -> None:
    set_bot_presentation_cache(
        BotPresentationConfig(text_overrides={'MAIN_MENU_ACTION_PROMPT': 'Что будем делать?'})
    )
    assert get_text_override('ru', 'MAIN_MENU_ACTION_PROMPT') == 'Что будем делать?'
    assert get_text_override('en', 'MAIN_MENU_ACTION_PROMPT') is None
    assert Texts('ru').t('MAIN_MENU_ACTION_PROMPT') == 'Что будем делать?'
    assert Texts('en').t('MAIN_MENU_ACTION_PROMPT') != 'Что будем делать?'


def test_semantic_emoji_override_only_marks_one_ru_key() -> None:
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={TOKEN: CUSTOM_ID}))

    target = decorate_localized_text('ru', 'PROMO_GROUP_DISCOUNT_TRAFFIC', '📊 Трафик')
    same_glyph_other_context = decorate_localized_text('ru', 'UNRELATED_STATUS', '📊 Диагностика')
    non_russian = decorate_localized_text('en', 'PROMO_GROUP_DISCOUNT_TRAFFIC', '📊 Traffic')

    assert apply_html_emoji(target) == f'<tg-emoji emoji-id="{CUSTOM_ID}">📊</tg-emoji> Трафик'
    assert same_glyph_other_context == '📊 Диагностика'
    assert non_russian == '📊 Traffic'


def test_semantic_token_is_bound_to_upstream_fallback_glyph() -> None:
    stale_token = 'PROMO_GROUP_DISCOUNT_TRAFFIC#0:⚠️'
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={stale_token: CUSTOM_ID}))

    assert decorate_localized_text('ru', 'PROMO_GROUP_DISCOUNT_TRAFFIC', '📊 Трафик') == '📊 Трафик'


def test_texts_lookup_embeds_marker_only_for_target_ru_key() -> None:
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={TOKEN: CUSTOM_ID}))

    assert '\ue000' in Texts('ru').t('PROMO_GROUP_DISCOUNT_TRAFFIC')
    assert '\ue000' not in Texts('ru').t('PROMO_GROUP_DISCOUNT_DEVICES')
    assert '\ue000' not in Texts('en').t('PROMO_GROUP_DISCOUNT_TRAFFIC')


def test_unicode_fallback_is_recoverable_from_marker() -> None:
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={TOKEN: CUSTOM_ID}))
    marked = decorate_localized_text('ru', 'PROMO_GROUP_DISCOUNT_TRAFFIC', '📊 Трафик')
    assert strip_presentation_markers(marked) == '📊 Трафик'


def test_emoji_extraction_keeps_flag_skin_tone_keycap_and_zwj_sequences() -> None:
    assert extract_emoji('↩️ 🇷🇺 👍🏻 1️⃣ 👨‍💻') == ['↩️', '🇷🇺', '👍🏻', '1️⃣', '👨‍💻']


def test_text_override_rejects_changed_html_structure() -> None:
    with pytest.raises(ValueError, match='HTML structure'):
        validate_text_override('<b>Трафик: {value}</b>', '<i>Трафик: {value}</i>')


def test_text_override_rejects_malformed_html() -> None:
    with pytest.raises(ValueError, match='invalid or unbalanced HTML'):
        validate_text_override('Обычный текст', 'Сломанный <b')


def test_text_override_rejects_changed_placeholder_or_emoji_contract() -> None:
    with pytest.raises(ValueError, match='placeholders'):
        validate_text_override('Сумма: {amount:.2f}', 'Сумма: {amount}')
    with pytest.raises(ValueError, match='Unicode emoji'):
        validate_text_override('📊 Трафик: {amount}', '⚠️ Трафик: {amount}')


def test_text_override_accepts_same_contract() -> None:
    validate_text_override('📊 Истекает через {days} дн.', '📊 Осталось {days} дней')


def test_inline_button_uses_icon_without_changing_callback() -> None:
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={TOKEN: CUSTOM_ID}))
    marked = decorate_localized_text('ru', 'PROMO_GROUP_DISCOUNT_TRAFFIC', '📊 Трафик')
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=marked, callback_data='menu_traffic')]]
    )

    button = apply_button_presentation(keyboard).inline_keyboard[0][0]
    assert button.text == 'Трафик'
    assert button.icon_custom_emoji_id == CUSTOM_ID
    assert button.callback_data == 'menu_traffic'


def test_reply_keyboard_supports_semantic_icon_without_layout_change() -> None:
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={TOKEN: CUSTOM_ID}))
    marked = decorate_localized_text('ru', 'PROMO_GROUP_DISCOUNT_TRAFFIC', '📊 Трафик')
    keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=marked)]])

    result = apply_button_presentation(keyboard)
    assert len(result.keyboard) == 1
    assert result.keyboard[0][0].text == 'Трафик'
    assert result.keyboard[0][0].icon_custom_emoji_id == CUSTOM_ID


def test_button_with_explicit_custom_icon_keeps_existing_icon() -> None:
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={TOKEN: CUSTOM_ID}))
    marked = decorate_localized_text('ru', 'PROMO_GROUP_DISCOUNT_TRAFFIC', '📊 Трафик')
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=marked,
                icon_custom_emoji_id='999',
                callback_data='menu_traffic',
            )
        ]]
    )

    rendered = apply_button_presentation(keyboard)
    fallback = apply_button_presentation(keyboard, custom=False)
    button = rendered.inline_keyboard[0][0]
    fallback_button = fallback.inline_keyboard[0][0]
    assert button.text == '📊 Трафик'
    assert button.icon_custom_emoji_id == '999'
    assert fallback_button.text == '📊 Трафик'
    assert fallback_button.icon_custom_emoji_id == '999'
