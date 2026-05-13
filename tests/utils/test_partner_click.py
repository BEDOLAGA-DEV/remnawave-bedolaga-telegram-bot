"""Тесты валидатора click_id для партнёрских ссылок (Keitaro etc.)."""

import pytest

from app.utils.partner_click import CLICK_ID_PATTERN, CLICK_ID_RE, is_valid_click_id


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
