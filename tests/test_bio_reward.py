"""Smoke tests for bio-reward pure helpers (matching + recalc math)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.bio_reward_service import (
    bio_matches,
    build_personal_referral_tokens,
    expand_bio_template,
    recalc_paid_sub_on_revoke,
)


def _cfg(strings: list[str], match_personal: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        accepted_bio_strings=strings,
        match_personal_referral_link=match_personal,
    )


# ---------- bio_matches ----------


def test_bio_matches_substring_case_insensitive():
    cfg = _cfg(['Я пользуюсь VPN от @nozapretbot'])
    assert bio_matches('Hello! Я пользуюсь VPN от @nozapretbot — рекомендую', cfg, [])
    assert bio_matches('я пользуюсь vpn от @NOZAPRETBOT', cfg, [])


def test_bio_matches_no_text():
    cfg = _cfg(['target'])
    assert bio_matches(None, cfg, []) is False
    assert bio_matches('', cfg, []) is False


def test_bio_matches_personal_referral_token():
    cfg = _cfg(['canonical-string'])
    tokens = ['ABC123', 'start=ABC123', 'start=abc123']
    assert bio_matches('check my link t.me/foo?start=ABC123', cfg, tokens)
    assert bio_matches('contains code ABC123 alone', cfg, tokens)


def test_bio_matches_disabled_personal_link():
    cfg = _cfg(['canonical-string'], match_personal=False)
    assert bio_matches('start=ABC123 only', cfg, ['ABC123', 'start=ABC123']) is False


def test_build_personal_referral_tokens_no_code():
    user = SimpleNamespace(referral_code=None)
    assert build_personal_referral_tokens(user) == []
    user2 = SimpleNamespace(referral_code='   ')
    assert build_personal_referral_tokens(user2) == []


def test_build_personal_referral_tokens_with_code():
    user = SimpleNamespace(referral_code='AbC123')
    tokens = build_personal_referral_tokens(user)
    assert 'AbC123' in tokens
    assert 'start=AbC123' in tokens
    assert 'start=abc123' in tokens


# ---------- recalc_paid_sub_on_revoke ----------


@pytest.fixture
def start_date():
    return datetime(2026, 5, 1, tzinfo=UTC)


def test_recalc_under_used_shrinks_end_date(start_date):
    """Bought 30d at 80₽ (20% off, full=100₽). Used 5 days. Should shrink to 24 days."""
    result = recalc_paid_sub_on_revoke(
        paid_kopeks=8000,
        discount_percent=20,
        total_days=30,
        start_date=start_date,
        now=start_date + timedelta(days=5),
    )
    assert result['debit_kopeks'] == 0
    assert result['entitled_days'] == 24
    assert result['new_end_date'] == start_date + timedelta(days=24)


def test_recalc_over_used_charges_balance(start_date):
    """Bought 30d at 80₽. Used 25 days. Entitled = 24 days. Over-used 1 day ≈ 333 kopeks debit."""
    result = recalc_paid_sub_on_revoke(
        paid_kopeks=8000,
        discount_percent=20,
        total_days=30,
        start_date=start_date,
        now=start_date + timedelta(days=25),
    )
    assert result['used_days'] == 25
    assert result['entitled_days'] == 24
    assert 330 <= result['debit_kopeks'] <= 335
    assert result['new_end_date'] == start_date + timedelta(days=25)


def test_recalc_exactly_entitled(start_date):
    """Used exactly entitled_days → no debit, end_date = now."""
    result = recalc_paid_sub_on_revoke(
        paid_kopeks=8000,
        discount_percent=20,
        total_days=30,
        start_date=start_date,
        now=start_date + timedelta(days=24),
    )
    assert result['used_days'] == 24
    assert result['entitled_days'] == 24
    assert result['debit_kopeks'] == 0


def test_recalc_zero_discount_is_passthrough(start_date):
    """Discount 0 → no recalc, original end_date preserved."""
    result = recalc_paid_sub_on_revoke(
        paid_kopeks=10000,
        discount_percent=0,
        total_days=30,
        start_date=start_date,
        now=start_date + timedelta(days=10),
    )
    assert result['debit_kopeks'] == 0
    assert result['new_end_date'] == start_date + timedelta(days=30)


def test_recalc_invalid_inputs_safe(start_date):
    """Edge: zero days or zero kopeks should not crash."""
    for total_days in (0, 30):
        for paid in (0, 8000):
            result = recalc_paid_sub_on_revoke(
                paid_kopeks=paid,
                discount_percent=20,
                total_days=total_days,
                start_date=start_date,
                now=start_date,
            )
            assert 'new_end_date' in result
            assert result['debit_kopeks'] >= 0


# ---------- expand_bio_template + placeholder matching ----------


def test_expand_bot_username_and_mention():
    user = SimpleNamespace(referral_code='REF123')
    out = expand_bio_template(
        'VPN от {{bot_mention}} ({{bot_username}})', bot_username='nozapretbot', user=user
    )
    assert out == 'VPN от @nozapretbot (nozapretbot)'


def test_expand_user_ref_and_link():
    user = SimpleNamespace(referral_code='REF123')
    out = expand_bio_template(
        'Бот {{bot_mention}} | мой код {{user_ref}} | ссылка {{user_ref_link}}',
        bot_username='nozapretbot',
        user=user,
    )
    assert out == 'Бот @nozapretbot | мой код REF123 | ссылка https://t.me/nozapretbot?start=REF123'


def test_expand_missing_user_returns_empty_placeholders():
    out = expand_bio_template('a {{user_ref}} b {{user_ref_link}}', bot_username='bot', user=None)
    assert out == 'a  b '


def test_bio_matches_with_placeholder_template():
    cfg = _cfg(['Я пользуюсь VPN от {{bot_mention}}'])
    user = SimpleNamespace(referral_code='REF123')
    bio = 'Hi! Я пользуюсь VPN от @nozapretbot — рекомендую'
    assert bio_matches(bio, cfg, [], bot_username='nozapretbot', user=user)


def test_bio_matches_user_ref_via_template():
    cfg = _cfg(['Лучший VPN {{user_ref_link}}'])
    user = SimpleNamespace(referral_code='ABC')
    bio = 'Лучший VPN https://t.me/nozapretbot?start=ABC'
    assert bio_matches(bio, cfg, [], bot_username='nozapretbot', user=user)


def test_bio_matches_template_misses_when_placeholder_unresolved():
    cfg = _cfg(['Code {{user_ref}}'])
    # No user → user_ref expands to '' → rendered = 'Code ' which is whitespace-trimmable, skipped
    user = SimpleNamespace(referral_code=None)
    bio = 'Code anything'
    assert bio_matches(bio, cfg, [], bot_username='bot', user=user) is False
