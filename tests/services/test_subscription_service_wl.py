"""Unit tests for SubscriptionService WL username handling.

Tests cover the rule that WL primary username mirrors the actual main-account
username on the RemnaWave panel (legacy 'user_<tg>' or new 'u_<tg>_<sub_id>'),
and that duplicate WL accounts in the other format get cleaned up on sync.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.subscription_service import SubscriptionService


def _make_user(telegram_id: int = 123, user_id: int = 1) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.telegram_id = telegram_id
    user.username = 'tester'
    user.full_name = 'Test User'
    user.email = None
    user.language = 'ru'
    return user


def _make_subscription(sub_id: int = 42, tariff=None) -> MagicMock:
    sub = MagicMock()
    sub.id = sub_id
    sub.tariff_id = 7 if tariff else None
    sub.tariff = tariff
    sub.wl_traffic_limit_gb = 50
    sub.wl_traffic_used_gb = 0.0
    sub.end_date = MagicMock()
    sub.status = 'active'
    return sub


@pytest.mark.asyncio
async def test_primary_wl_username_built_from_main_username_legacy_form():
    """When main is adopted as legacy 'user_<tg>', WL must be 'user_<tg>_wl'."""
    service = SubscriptionService()
    api = MagicMock()
    api.get_user_by_username = AsyncMock(return_value=None)
    api.create_user = AsyncMock(return_value=types.SimpleNamespace(uuid='new-wl-uuid'))
    api.delete_user = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.create_user.await_count == 1
    create_kwargs = api.create_user.await_args.kwargs
    assert create_kwargs['username'] == 'user_123_wl'
