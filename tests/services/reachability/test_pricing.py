"""Деньги: 1 кредит = 1 копейка; VLESS без preview оценивается по цене лега; потолок задачи."""

import pytest

from app.services.reachability.pricing import (
    DEFAULT_VLESS_LEG_KOPEKS,
    CostLimitExceeded,
    credits_to_kopeks,
    enforce_cost_limit,
    estimate_vless_kopeks,
    format_rubles,
)


def test_credits_are_kopeks() -> None:
    assert credits_to_kopeks(279) == 279
    assert credits_to_kopeks(None) is None


def test_vless_estimate_uses_last_leg_price_or_default() -> None:
    assert estimate_vless_kopeks(2, 3, 103) == 618
    assert estimate_vless_kopeks(1, 1, None) == DEFAULT_VLESS_LEG_KOPEKS


def test_cost_limit_zero_means_unlimited() -> None:
    enforce_cost_limit(10_000_000, 0)
    enforce_cost_limit(None, 500)


def test_cost_limit_exceeded_carries_numbers() -> None:
    with pytest.raises(CostLimitExceeded) as exc:
        enforce_cost_limit(501, 500)
    assert (exc.value.cost_kopeks, exc.value.limit_kopeks) == (501, 500)
    enforce_cost_limit(500, 500)


def test_format_rubles() -> None:
    assert format_rubles(279) == '2,79 ₽'
    assert format_rubles(100000) == '1000,00 ₽'
