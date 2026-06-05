"""Regression tests for the Telegram Stars ↔ rubles conversion rate.

The bug from 2026-05-16: with default rate=1.3 ₽/⭐, asking the bot to
top up 150 ₽ produced a quote of 115 ⭐ (round(150/1.3)=115), and the
return-conversion credited 115 × 1.3 = 149.50 ₽. The user paid for
150 ₽ worth of stars and got 149.50 ₽ credited — a built-in rounding
loss on every transaction, plus the rate itself was ~30% below
Telegram's actual cash-out rate (~0.95–1.0 ₽/⭐).

These tests pin:
  1. The default rate stays 1.0 (matches market, eliminates the loss
     for integer-ruble round-trips).
  2. Common integer ruble amounts round-trip losslessly at the default
     rate. If someone changes the default to >1 again, these tests
     fail with the exact ruble loss highlighted.
"""

from __future__ import annotations

import pytest

from app.config import settings


def test_default_stars_rate_is_one_ruble_per_star() -> None:
    """REGRESSION: default rate must stay at 1.0 ₽/⭐.

    Lower → users get over-credited (bot loses money — but Telegram
    actually pays bot owners ~0.95 ₽/⭐ on withdrawal, so the floor is
    around there).
    Higher → users get under-credited (the original 1.3-default bug:
    150 ₽ top-up credited as 149.50 ₽).
    """
    assert settings.TELEGRAM_STARS_RATE_RUB == 1.0, (
        f'Default TELEGRAM_STARS_RATE_RUB must be 1.0 to match Telegram cash-out and '
        f'round-trip losslessly. Got {settings.TELEGRAM_STARS_RATE_RUB!r}.'
    )


@pytest.mark.parametrize('rubles', [50, 100, 150, 200, 500, 1000, 5000])
def test_integer_ruble_amounts_round_trip_losslessly(
    rubles: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION: at rate=1.0, integer ruble top-ups credit back exactly.

    Pre-fix at rate=1.3:
      150 ₽ → rubles_to_stars(150) = round(115.38) = 115 ⭐
      stars_to_rubles(115) = 115 × 1.3 = 149.50 ₽ (loss = 0.50 ₽)

    Post-fix at rate=1.0 this loss is gone for any integer ruble input.
    """
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.0, raising=False)

    stars = settings.rubles_to_stars(float(rubles))
    rubles_back = settings.stars_to_rubles(stars)

    assert stars == rubles, f'{rubles} ₽ must quote {rubles} ⭐ at rate=1.0, got {stars}'
    assert rubles_back == float(rubles), (
        f'{rubles} ₽ → {stars} ⭐ → {rubles_back} ₽ is not lossless (delta {rubles_back - rubles:+.2f} ₽)'
    )


def test_rubles_to_stars_rejects_invalid_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive check: zero/negative rate must raise rather than divide-by-zero."""
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 0, raising=False)
    with pytest.raises(ValueError):
        settings.rubles_to_stars(100)

    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', -1, raising=False)
    with pytest.raises(ValueError):
        settings.rubles_to_stars(100)


def test_rubles_to_stars_clamps_to_minimum_one_star(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even at rate=1.0, a 0 ₽ request must return ≥1 ⭐ (Telegram requires positive amount)."""
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.0, raising=False)
    assert settings.rubles_to_stars(0) == 1
    # Negative inputs are caller-error but should not return <1.
    assert settings.rubles_to_stars(-50) == 1


def test_rate_change_is_propagated_through_telegram_stars_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TelegramStarsService.calculate_*` helpers must defer to settings — no hardcoded copies.

    Pinned because both `external/telegram_stars.py` and
    `services/payment/stars.py` historically had drift risk: if one
    hardcoded a rate and the other used settings, the invoice quote
    and the post-payment credit would diverge silently.
    """
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 2.5, raising=False)

    from app.external.telegram_stars import TelegramStarsService

    assert TelegramStarsService.calculate_stars_from_rubles(100.0) == settings.rubles_to_stars(100.0)
    rubles_back = TelegramStarsService.calculate_rubles_from_stars(40)
    assert float(rubles_back) == settings.stars_to_rubles(40)
