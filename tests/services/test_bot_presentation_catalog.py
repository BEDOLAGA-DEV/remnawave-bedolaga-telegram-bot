import pytest

from app.services.bot_presentation_catalog import (
    build_bot_presentation_catalog,
    validate_config_against_catalog,
)
from app.services.bot_presentation_service import BotPresentationConfig


def test_catalog_discovers_real_texts_accessed_by_attribute_and_t_call() -> None:
    catalog = build_bot_presentation_catalog()

    assert 'MAIN_MENU_ACTION_PROMPT' in catalog.texts
    assert any('app/handlers/menu.py:' in usage for usage in catalog.texts['MAIN_MENU_ACTION_PROMPT'].usages)
    assert 'MENU_BALANCE' in catalog.texts
    assert catalog.texts['MENU_BALANCE'].usages
    assert 'ADMIN_POLLS_CUSTOM_PROMPT' in catalog.texts
    assert catalog.texts['BACK'].usage_count > len(catalog.texts['BACK'].usages)
    assert len(catalog.texts['BACK'].usages) == 20


def test_catalog_excludes_non_output_rule_and_diagnostic_literals() -> None:
    catalog = build_bot_presentation_catalog()

    assert 'RULES_TEXT' not in catalog.texts
    assert 'TRAFFIC_5GB' not in catalog.texts
    assert all('app/bot.py:90' not in usage for item in catalog.emoji.values() for usage in item.usages)


def test_catalog_emoji_tokens_are_semantic_and_unicode_sequences_are_complete() -> None:
    catalog = build_bot_presentation_catalog()
    item = catalog.emoji['PROMO_GROUP_DISCOUNT_TRAFFIC#0:📊']

    assert item.localization_key == 'PROMO_GROUP_DISCOUNT_TRAFFIC'
    assert item.glyph == '📊'
    assert item.usages
    assert all(token.rsplit('#', 1)[0] in catalog.texts for token in catalog.emoji)


def test_config_validation_rejects_unknown_semantic_token() -> None:
    with pytest.raises(ValueError, match='unknown semantic emoji token'):
        validate_config_against_catalog(
            BotPresentationConfig(emoji_overrides={'UNKNOWN#0:❓': '5368324170671202286'})
        )


def test_config_validation_rejects_lost_text_placeholder() -> None:
    with pytest.raises(ValueError, match='days'):
        validate_config_against_catalog(
            BotPresentationConfig(text_overrides={'MAIN_MENU_RICH_DAYS_LEFT': 'Осталось немного'})
        )
