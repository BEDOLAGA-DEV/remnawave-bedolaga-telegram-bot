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
    sub.device_limit = None
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


@pytest.mark.asyncio
async def test_primary_wl_username_built_from_main_username_new_form():
    """When main is created as 'u_<tg>_<sub_id>', WL must be 'u_<tg>_<sub_id>_wl'."""
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
        main_username='u_123_42',
    )

    create_kwargs = api.create_user.await_args.kwargs
    assert create_kwargs['username'] == 'u_123_42_wl'


@pytest.mark.asyncio
async def test_primary_wl_username_truncated_when_main_too_long():
    """Main usernames longer than 33 chars are truncated before appending _wl."""
    service = SubscriptionService()
    api = MagicMock()
    api.get_user_by_username = AsyncMock(return_value=None)
    api.create_user = AsyncMock(return_value=types.SimpleNamespace(uuid='new-wl-uuid'))
    api.delete_user = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)
    long_main = 'a' * 50  # 50 chars

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username=long_main,
    )

    final = api.create_user.await_args.kwargs['username']
    assert final.endswith('_wl')
    assert len(final) <= 36
    assert final == 'a' * 33 + '_wl'


@pytest.mark.asyncio
async def test_cleanup_deletes_orphan_legacy_wl_when_primary_is_new_form():
    """If main is new-form, the legacy 'user_<tg>_wl' orphan must be deleted."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')
    legacy_orphan = types.SimpleNamespace(uuid='legacy-wl-uuid')

    async def fake_get(username: str):
        if username == 'u_123_42_wl':
            return primary_wl_user
        if username == 'user_123_wl':
            return legacy_orphan
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(return_value=True)
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='u_123_42',
    )

    assert api.delete_user.await_count == 1
    api.delete_user.assert_awaited_with('legacy-wl-uuid')


@pytest.mark.asyncio
async def test_cleanup_deletes_orphan_new_form_when_primary_is_legacy():
    """If main is legacy, the new-form 'u_<tg>_<sub_id>_wl' orphan must be deleted."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')
    new_orphan = types.SimpleNamespace(uuid='orphan-uuid')

    async def fake_get(username: str):
        if username == 'user_123_wl':
            return primary_wl_user
        if username == 'u_123_42_wl':
            return new_orphan
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(return_value=True)
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.delete_user.await_count == 1
    api.delete_user.assert_awaited_with('orphan-uuid')


@pytest.mark.asyncio
async def test_cleanup_no_duplicates_no_delete():
    """When no duplicate exists, delete_user must not be called."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')

    async def fake_get(username: str):
        if username == 'user_123_wl':
            return primary_wl_user
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(return_value=True)
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.delete_user.await_count == 0


@pytest.mark.asyncio
async def test_cleanup_delete_failure_does_not_break_flow():
    """If delete_user raises, the sync still completes."""
    service = SubscriptionService()
    api = MagicMock()
    primary_wl_user = types.SimpleNamespace(uuid='primary-wl-uuid')
    orphan = types.SimpleNamespace(uuid='orphan-uuid')

    async def fake_get(username: str):
        if username == 'user_123_wl':
            return primary_wl_user
        if username == 'u_123_42_wl':
            return orphan
        return None

    api.get_user_by_username = AsyncMock(side_effect=fake_get)
    api.update_user = AsyncMock(return_value=primary_wl_user)
    api.delete_user = AsyncMock(side_effect=Exception('boom'))
    api.reset_user_devices = AsyncMock(return_value=True)

    user = _make_user(telegram_id=123)
    subscription = _make_subscription(sub_id=42)

    # Must NOT raise.
    await service._ensure_wl_user_synced(
        api,
        user,
        subscription,
        is_actually_active=True,
        main_username='user_123',
    )

    assert api.delete_user.await_count == 1
