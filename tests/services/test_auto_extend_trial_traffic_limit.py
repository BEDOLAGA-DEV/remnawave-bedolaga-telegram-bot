"""Regression tests for Telegram bug report #654380.

«После обновления при переходе с триала на платную остаётся 10 ГБ трафика с триала
в подписке, хотя платная безлимит.»

Root cause: the 'extend'-mode autopay (`_auto_extend_subscription`) carried the trial's
`TRIAL_TRAFFIC_LIMIT_GB` into the converted paid subscription. The renewal cart has no
`traffic_limit_gb` (it's a renewal) and a trial's `tariff_id` usually matches the target
tariff, so `is_tariff_change` is False and `extend_subscription` was never given the paid
tariff's limit — leaving the trial cap (10 GB) even on an unlimited (0) tariff.

`_resolve_extend_traffic_limit_gb` now resolves the paid tariff's limit on a trial
conversion (preserving an explicit cart value, and treating 0 as unlimited — NOT as a
falsy "unset").
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import subscription_auto_purchase_service as svc


@pytest.mark.asyncio
async def test_trial_conversion_adopts_unlimited_tariff_limit(monkeypatch):
    """The reported case: unlimited (0) paid tariff must win over the trial's 10 GB."""
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id',
        AsyncMock(return_value=SimpleNamespace(id=5, traffic_limit_gb=0)),
    )
    prepared = SimpleNamespace(traffic_limit_gb=None, tariff_id=5)  # renewal cart omits traffic
    subscription = SimpleNamespace(tariff_id=5)  # trial on the same tariff -> is_tariff_change False

    result = await svc._resolve_extend_traffic_limit_gb(AsyncMock(), prepared, subscription, was_trial=True)

    assert result == 0  # unlimited from the paid tariff, NOT the trial cap


@pytest.mark.asyncio
async def test_trial_conversion_falls_back_to_subscription_tariff_id(monkeypatch):
    """When the cart has no tariff_id, resolve via the subscription's tariff_id."""
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id',
        AsyncMock(return_value=SimpleNamespace(id=5, traffic_limit_gb=200)),
    )
    prepared = SimpleNamespace(traffic_limit_gb=None, tariff_id=None)
    subscription = SimpleNamespace(tariff_id=5)

    result = await svc._resolve_extend_traffic_limit_gb(AsyncMock(), prepared, subscription, was_trial=True)

    assert result == 200


@pytest.mark.asyncio
async def test_explicit_cart_traffic_is_preserved(monkeypatch):
    """An explicit (custom) cart traffic value must NOT be overwritten by the tariff."""
    spy = AsyncMock(return_value=SimpleNamespace(id=5, traffic_limit_gb=0))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', spy)
    prepared = SimpleNamespace(traffic_limit_gb=150, tariff_id=5)
    subscription = SimpleNamespace(tariff_id=5)

    result = await svc._resolve_extend_traffic_limit_gb(AsyncMock(), prepared, subscription, was_trial=True)

    assert result == 150
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_non_trial_renewal_is_untouched(monkeypatch):
    """Ordinary (non-trial) renewal: no tariff lookup, return the cart value as-is."""
    spy = AsyncMock(return_value=SimpleNamespace(id=5, traffic_limit_gb=0))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', spy)
    prepared = SimpleNamespace(traffic_limit_gb=None, tariff_id=5)
    subscription = SimpleNamespace(tariff_id=5)

    result = await svc._resolve_extend_traffic_limit_gb(AsyncMock(), prepared, subscription, was_trial=False)

    assert result is None
    spy.assert_not_called()
