"""Invariant tests for the gift deep-link token prefix threshold.

A gift deep-link (``GIFT_<token>`` / ``giftclaim_<token>``) overflows Telegram's 64-char
start_param limit, so Telegram truncates the token by the prefix length. Prefix-based gift
lookups therefore accept a *truncated* token — but the minimum accepted length must be high
enough that a short, guessable prefix can't claim an arbitrary gift. These tests pin that the
threshold accepts every legitimately-truncated token yet rejects the old too-short floor.
"""

from __future__ import annotations

import urllib.parse

from app.database.crud.landing import generate_purchase_token
from app.services.guest_purchase_service import GIFT_TOKEN_MIN_PREFIX_LENGTH
from app.utils.gift_links import build_bot_gift_claim_link


TELEGRAM_START_PARAM_MAX = 64


def test_generated_token_is_64_chars() -> None:
    # GuestPurchase.token is String(64); token_urlsafe(48) yields exactly 64 url-safe chars.
    assert len(generate_purchase_token()) == TELEGRAM_START_PARAM_MAX


def test_threshold_accepts_every_legitimate_truncation() -> None:
    token = generate_purchase_token()
    # Telegram keeps the first (64 - prefix_len) chars of "<prefix><token>".
    for prefix in ('GIFT_', 'giftclaim_'):
        surviving = (prefix + token)[:TELEGRAM_START_PARAM_MAX]
        truncated_token = surviving[len(prefix) :]
        assert len(truncated_token) >= GIFT_TOKEN_MIN_PREFIX_LENGTH, (
            f'{prefix!r} truncation leaves {len(truncated_token)} chars, '
            f'below the {GIFT_TOKEN_MIN_PREFIX_LENGTH} threshold'
        )


def test_threshold_rejects_the_old_short_floor() -> None:
    # The previous 8-char floor allowed enumeration of arbitrary gifts.
    assert GIFT_TOKEN_MIN_PREFIX_LENGTH > 8
    assert len('X' * 8) < GIFT_TOKEN_MIN_PREFIX_LENGTH


def test_canonical_bot_gift_claim_link_meets_threshold() -> None:
    """Canonical build_bot_gift_claim_link produces links accepted by the threshold."""
    token = generate_purchase_token()
    link = build_bot_gift_claim_link(token, 'my_bot')

    parsed = urllib.parse.urlparse(link)
    start_param = urllib.parse.parse_qs(parsed.query)['start'][0]

    assert len(start_param) <= TELEGRAM_START_PARAM_MAX
    assert start_param.startswith('GIFT_')

    gift_token = start_param[5:]
    assert len(gift_token) >= GIFT_TOKEN_MIN_PREFIX_LENGTH
    assert len(gift_token) == 59
    assert token.startswith(gift_token)


def test_legacy_12_char_slice_is_rejected_by_threshold() -> None:
    """Legacy 12-char slice is strictly rejected by the 48-char security threshold."""
    token = generate_purchase_token()
    legacy_param = f'GIFT_{token[:12]}'
    legacy_token = legacy_param[5:]

    assert len(legacy_token) == 12
    assert len(legacy_token) < GIFT_TOKEN_MIN_PREFIX_LENGTH
