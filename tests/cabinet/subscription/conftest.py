# tests/cabinet/subscription/conftest.py
"""Shared fixtures for cabinet subscription/traffic tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def make_user():
    """Build a mock User row with the fields traffic logic touches."""
    def _make(*, id: int = 1, balance_kopeks: int = 10_000_000, telegram_id: int = 1234567890):
        u = SimpleNamespace()
        u.id = id
        u.telegram_id = telegram_id
        u.balance_kopeks = balance_kopeks
        u.remnawave_uuid = 'main-uuid'
        u.restriction_subscription = False
        return u
    return _make


@pytest.fixture
def make_subscription():
    """Build a mock Subscription row. Defaults to a paid subscription with 50GB regular and 50GB WL."""
    def _make(
        *,
        id: int = 1,
        user_id: int = 1,
        is_trial: bool = False,
        status: str = 'active',
        traffic_limit_gb: int = 50,
        traffic_used_gb: float = 10.0,
        purchased_traffic_gb: int = 0,
        wl_traffic_limit_gb: int = 50,
        wl_traffic_used_gb: float = 5.0,
        wl_purchased_traffic_gb: int = 0,
        tariff_id: int | None = None,
        days_left: int = 30,
        remnawave_uuid: str = 'sub-uuid',
    ):
        s = SimpleNamespace()
        s.id = id
        s.user_id = user_id
        s.is_trial = is_trial
        s.status = status
        s.traffic_limit_gb = traffic_limit_gb
        s.traffic_used_gb = traffic_used_gb
        s.purchased_traffic_gb = purchased_traffic_gb
        s.traffic_reset_at = None
        s.wl_traffic_limit_gb = wl_traffic_limit_gb
        s.wl_traffic_used_gb = wl_traffic_used_gb
        s.wl_purchased_traffic_gb = wl_purchased_traffic_gb
        s.wl_traffic_reset_at = None
        s.tariff_id = tariff_id
        s.end_date = datetime.now(UTC) + timedelta(days=days_left)
        s.start_date = datetime.now(UTC) - timedelta(days=1)
        s.remnawave_uuid = remnawave_uuid
        s.updated_at = datetime.now(UTC)
        return s
    return _make


@pytest.fixture
def mock_db():
    """Mock AsyncSession with the methods our code calls."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return db
