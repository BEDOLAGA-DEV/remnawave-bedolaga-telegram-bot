"""Fix (#1): switching from a free (0₽) tariff to a paid one must NOT carry over the
days the user spammed on the free tier. Paid→paid switches still preserve days.

The decision lives in extend_subscription via two pure-ish helpers:
- Tariff.is_free  → is the source tariff actually free?
- _should_carry_remaining_days(is_trial, source_is_free) → carry the remaining days?
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import app.database.crud.subscription as sub_crud
from app.database.models import Tariff


# ── Tariff.is_free ──


def test_is_free_paid_periodic():
    assert Tariff(is_daily=False, period_prices={'30': 9900, '90': 26900}, daily_price_kopeks=0).is_free is False


def test_is_free_zero_periodic():
    assert Tariff(is_daily=False, period_prices={'30': 0, '90': 0}, daily_price_kopeks=0).is_free is True


def test_is_free_mixed_is_not_free():
    # одна платная цена → тариф не бесплатный
    assert Tariff(is_daily=False, period_prices={'30': 0, '90': 9900}, daily_price_kopeks=0).is_free is False


def test_is_free_empty_prices_not_free():
    # неопределённость трактуем как «не бесплатный» (безопасно — дни перенесутся)
    assert Tariff(is_daily=False, period_prices={}, daily_price_kopeks=0).is_free is False


def test_is_free_daily_zero():
    assert Tariff(is_daily=True, period_prices={}, daily_price_kopeks=0).is_free is True


def test_is_free_daily_paid():
    assert Tariff(is_daily=True, period_prices={}, daily_price_kopeks=5000).is_free is False


# ── _should_carry_remaining_days ──


def test_paid_sub_carries_days():
    assert sub_crud._should_carry_remaining_days(is_trial=False, source_is_free=False) is True


def test_free_source_does_not_carry():
    assert sub_crud._should_carry_remaining_days(is_trial=False, source_is_free=True) is False


def test_trial_does_not_carry_by_default(monkeypatch):
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    assert sub_crud._should_carry_remaining_days(is_trial=True, source_is_free=False) is False


def test_trial_carries_when_setting_on(monkeypatch):
    monkeypatch.setattr(sub_crud.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    assert sub_crud._should_carry_remaining_days(is_trial=True, source_is_free=False) is True


# ── _is_free_source_tariff (DB lookup wrapper) ──


async def test_is_free_source_tariff_true(monkeypatch):
    import app.database.crud.tariff as tariff_crud

    free = Tariff(is_daily=False, period_prices={'30': 0}, daily_price_kopeks=0)
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=free))
    assert await sub_crud._is_free_source_tariff(AsyncMock(), 5) is True


async def test_is_free_source_tariff_false_for_paid(monkeypatch):
    import app.database.crud.tariff as tariff_crud

    paid = Tariff(is_daily=False, period_prices={'30': 9900}, daily_price_kopeks=0)
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=paid))
    assert await sub_crud._is_free_source_tariff(AsyncMock(), 5) is False


async def test_is_free_source_tariff_handles_missing(monkeypatch):
    import app.database.crud.tariff as tariff_crud

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=None))
    assert await sub_crud._is_free_source_tariff(AsyncMock(), 5) is False


async def test_is_free_source_tariff_safe_on_error(monkeypatch):
    """Любая ошибка lookup → False (переносим дни как раньше, смена не падает)."""
    import app.database.crud.tariff as tariff_crud

    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(side_effect=RuntimeError('db down')))
    assert await sub_crud._is_free_source_tariff(AsyncMock(), 5) is False
