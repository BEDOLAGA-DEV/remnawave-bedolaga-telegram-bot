"""Тесты валидатора click_id для партнёрских ссылок (Keitaro etc.)."""

import pytest

from app.utils.partner_click import (
    CLICK_ID_PATTERN,
    CLICK_ID_RE,
    extract_click_id_from_start_param,
    is_valid_click_id,
)


@pytest.mark.parametrize(
    'value,is_valid',
    [
        ('abc123', True),
        ('AaZz09._:-', True),
        ('keitaro_click_id_123', True),
        ('a' * 128, True),
        ('a' * 129, False),
        ('', False),
        (None, False),
        ('with space', False),
        ('emoji-😀', False),
        ('semicolon;injection', False),
        ('slash/path', False),
        ('plus+sign', False),
        ('hash#sign', False),
        ('quote"sign', False),
        ('lt<sign', False),
    ],
)
def test_is_valid_click_id(value: str | None, is_valid: bool) -> None:
    assert is_valid_click_id(value) is is_valid


def test_pattern_anchored_full_match() -> None:
    """Pattern must reject partial matches via anchors `^...$`."""
    assert CLICK_ID_RE.match('valid_only_at_start space-at-end') is None
    assert CLICK_ID_RE.match('') is None


def test_pattern_string_exported() -> None:
    """CLICK_ID_PATTERN is exposed so cabinet pydantic models can reuse it."""
    assert CLICK_ID_PATTERN.startswith('^') and CLICK_ID_PATTERN.endswith('$')


# ---------- extract_click_id_from_start_param ----------


@pytest.mark.parametrize(
    'payload,expected_campaign,expected_click_id',
    [
        # Happy path: simple campaign_clk_clickid
        ('summer_clk_abc123', 'summer', 'abc123'),
        # No click_id marker — payload returned unchanged
        ('plain_campaign', 'plain_campaign', None),
        # Empty / None
        (None, None, None),
        ('', '', None),
        # Campaign part itself contains _clk_ — rsplit on rightmost separator
        ('my_clk_campaign_clk_realClickId', 'my_clk_campaign', 'realClickId'),
        # Only click_id, no campaign prefix
        ('_clk_lonelyClick', None, 'lonelyClick'),
        # Click part fails validation — payload returned untouched
        ('campaign_clk_has space', 'campaign_clk_has space', None),
        ('campaign_clk_', 'campaign_clk_', None),
        # Click part too long (>128) — invalid, fall through
        ('campaign_clk_' + 'a' * 129, 'campaign_clk_' + 'a' * 129, None),
        # Valid 128-char max
        ('camp_clk_' + 'a' * 128, 'camp', 'a' * 128),
    ],
)
def test_extract_click_id_from_start_param(
    payload: str | None,
    expected_campaign: str | None,
    expected_click_id: str | None,
) -> None:
    campaign, click_id = extract_click_id_from_start_param(payload)
    assert campaign == expected_campaign
    assert click_id == expected_click_id
