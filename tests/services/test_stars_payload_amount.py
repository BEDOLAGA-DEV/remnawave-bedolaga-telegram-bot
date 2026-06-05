"""Tests for Stars payment payload-amount recovery.

Background
----------
Pre-fix, ``process_stars_payment`` credited ``stars × rate`` and ignored
the originally-requested ``amount_kopeks`` encoded in the Telegram
invoice payload. At rate=1.0 with integer rubles the back-conversion
happens to be lossless, but:

  * Non-integer ruble inputs (e.g. 50.50 ₽) → ``round(50.50/1.0) = 50``
    stars → credit 50.00 ₽. User loses 0.50 ₽ per top-up.
  * Operator-set non-1.0 rates re-introduce the original 1.3-era class
    of bug.

Post-fix, the payload-encoded amount is the source of truth. These
tests pin:
  1. The parser handles every payload format the bot/miniapp/cabinet
     emit today.
  2. The plausibility bound rejects pathological amounts.
  3. Sub-ruble fractions survive a Stars top-up round trip.
"""

from __future__ import annotations

import pytest

from app.services.payment.stars import TelegramStarsMixin


parse = TelegramStarsMixin._parse_balance_topup_kopeks
plausible = TelegramStarsMixin._is_payload_amount_plausible


# ---------------------------------------------------------------------------
# Parser — every payload format must yield the original amount_kopeks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'payload,expected',
    [
        # Bot path: app/services/payment/stars.py
        ('balance_topup_15000', 15000),
        ('balance_topup_50', 50),
        # Miniapp path: app/webapi/routes/miniapp.py:1272
        ('balance_123_15000', 15000),
        # Miniapp variant with suffix: app/webapi/routes/miniapp.py:406
        ('balance_123_15000_a1b2c3', 15000),
        # Cabinet path: app/cabinet/routes/balance.py:269
        ('balance_topup_123_15000_1735689600', 15000),
        # Fractional ruble survives because kopeks is the unit (50.50 ₽ = 5050).
        ('balance_topup_5050', 5050),
        ('balance_456_5050_abcdef', 5050),
    ],
)
def test_parser_extracts_amount_kopeks_from_known_payload_shapes(payload: str, expected: int) -> None:
    assert parse(payload) == expected, f'Parser must recover {expected} kopeks from {payload!r}; got {parse(payload)!r}'


@pytest.mark.parametrize(
    'payload',
    [
        '',
        None,
        'simple_sub_1_2_30',  # subscription payload — handled by a different parser
        'not_a_balance_payload',
        'balance',
        'balance_',
        'balance_topup',  # missing amount
        'balance_topup_notanumber',
        'balance_topup_-100',  # negative — rejected
        'balance_topup_0',  # zero — rejected
        'balance_topup_uid_alpha_ts',  # non-numeric kopeks slot
    ],
)
def test_parser_returns_none_for_unrecognised_shapes(payload: object) -> None:
    assert parse(payload) is None, f'Parser must return None for {payload!r} so caller falls back to stars×rate'


# ---------------------------------------------------------------------------
# Plausibility bound — defensive sanity check.
# ---------------------------------------------------------------------------


def test_plausibility_accepts_lossless_round_trip() -> None:
    """At rate=1.0 with integer rubles, payload == reconstructed exactly."""
    assert plausible(payload_kopeks=15000, reconstructed_kopeks=15000) is True


def test_plausibility_accepts_sub_ruble_drift() -> None:
    """50.50 ₽ requested → 50 ⭐ × 1.0 = 50.00 ₽ reconstructed → 50 kopeks drift, well within tolerance."""
    assert plausible(payload_kopeks=5050, reconstructed_kopeks=5000) is True


def test_plausibility_accepts_20pct_drift() -> None:
    """A 20% rate change between invoice creation and payment must NOT trip the guard."""
    assert plausible(payload_kopeks=12000, reconstructed_kopeks=10000) is True


def test_plausibility_rejects_inflated_payload() -> None:
    """A payload claiming 10× the reconstructed amount is pathological — fall back to stars×rate."""
    assert plausible(payload_kopeks=100000, reconstructed_kopeks=10000) is False


def test_plausibility_rejects_zero_or_negative() -> None:
    assert plausible(payload_kopeks=0, reconstructed_kopeks=10000) is False
    assert plausible(payload_kopeks=-500, reconstructed_kopeks=10000) is False
    assert plausible(payload_kopeks=10000, reconstructed_kopeks=0) is False


def test_plausibility_uses_minimum_100_kopek_floor_for_tiny_amounts() -> None:
    """For tiny amounts (e.g. 50 kopeks reconstructed), 20% would be 10 — too tight.

    The 100-kopek floor makes the guard permissive enough that legitimate
    fractional-ruble cases pass while still rejecting obvious garbage.
    """
    # Off by 100 — within floor.
    assert plausible(payload_kopeks=150, reconstructed_kopeks=50) is True
    # Off by 1000 — beyond floor and beyond 20%.
    assert plausible(payload_kopeks=1050, reconstructed_kopeks=50) is False


# ---------------------------------------------------------------------------
# Negative-control: at the old broken rate (1.3), the round-trip WAS lossy.
# This test documents the bug we set out to fix and proves the new
# default+payload-parse combination would have caught it.
# ---------------------------------------------------------------------------


def test_negative_control_old_rate_was_lossy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression cover: the pre-fix flow under rate=1.3 lost 0.50 ₽ on a 150 ₽ top-up.

    At rate=1.3:
      rubles_to_stars(150) = round(150 / 1.3) = 115 stars
      stars × 1.3 = 149.50 ₽ ≠ 150 ₽

    The plausibility bound also accepts the legitimate 50-kopek drift,
    so the payload-amount path would have credited the full 150 ₽
    instead of 149.50 ₽. This is the exact scenario from the
    2026-05-16 user report.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.3, raising=False)

    quoted_stars = settings.rubles_to_stars(150.0)
    reconstructed_rubles = settings.stars_to_rubles(quoted_stars)
    reconstructed_kopeks = round(reconstructed_rubles * 100)

    # Confirm the old bug WAS present at this rate.
    assert quoted_stars == 115, 'sanity check: 150 ₽ / 1.3 ₽/⭐ = 115 ⭐'
    assert reconstructed_rubles == 149.5, 'sanity check: 115 × 1.3 = 149.50 ₽ (the loss)'

    # Now confirm the new logic would recover the original amount:
    payload = 'balance_topup_15000'
    payload_kopeks = parse(payload)
    assert payload_kopeks == 15000

    # Plausibility holds: 50-kopek drift is well within the 20%-or-100 floor.
    assert plausible(payload_kopeks=payload_kopeks, reconstructed_kopeks=reconstructed_kopeks) is True


def test_negative_control_at_new_rate_is_lossless_for_integer_rubles(monkeypatch: pytest.MonkeyPatch) -> None:
    """At rate=1.0 with integer rubles, payload and reconstructed agree exactly."""
    from app.config import settings

    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.0, raising=False)

    for rubles in (50, 100, 150, 500, 1000):
        kopeks = rubles * 100
        quoted_stars = settings.rubles_to_stars(float(rubles))
        reconstructed_kopeks = round(settings.stars_to_rubles(quoted_stars) * 100)

        assert reconstructed_kopeks == kopeks, (
            f'rate=1.0 must round-trip integer rubles losslessly; {rubles} ₽ → '
            f'{quoted_stars} ⭐ → {reconstructed_kopeks / 100} ₽'
        )
        # Payload parse + plausibility hold.
        assert parse(f'balance_topup_{kopeks}') == kopeks
        assert plausible(payload_kopeks=kopeks, reconstructed_kopeks=reconstructed_kopeks) is True
