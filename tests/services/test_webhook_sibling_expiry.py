"""Regression: a RemnaWave user.deleted webhook (fired when one subscription's
panel user is deleted) must NOT expire the user's OTHER subscriptions unless the
panel POSITIVELY confirms their panel user is gone.

Bug: a user had a pre-MultiTariff subscription (its panel UUID stored on
user.remnawave_uuid), then bought + deleted a second one. Deleting the second
sub's panel user fired a user.deleted webhook whose sibling-expiry sweep
force-expired the ORIGINAL (still active, end_date far in the future) and wiped
its connected_squads — because the liveness check skipped the original
(other_sub.remnawave_uuid was empty in multi-tariff) and fell open to "expire".
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.remnawave_webhook_service as rw
from app.database.models import SubscriptionStatus


def _sub(sub_id: int, status: str, *, end_days: int, remnawave_uuid: str | None, squads: list[str]):
    return SimpleNamespace(
        id=sub_id,
        status=status,
        end_date=datetime.now(UTC) + timedelta(days=end_days),
        remnawave_uuid=remnawave_uuid,
        remnawave_short_uuid='short',
        connected_squads=list(squads),
        subscription_url='https://x',
        subscription_crypto_link='crypto',
        updated_at=datetime.now(UTC),
        is_trial=False,
    )


def _service():
    # These guards are class-level dicts shared across instances — reset per test.
    rw.RemnaWaveWebhookService._recent_recreations.clear()
    rw.RemnaWaveWebhookService._intentional_panel_deletions_by_uuid.clear()
    rw.RemnaWaveWebhookService._intentional_panel_deletions_by_telegram_id.clear()
    svc = rw.RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    svc._get_renew_keyboard = MagicMock(return_value=None)
    svc._stamp_webhook_update = MagicMock()
    return svc


def _panel_service(panel_user=None, error: Exception | None = None):
    api = MagicMock()
    api.get_user_by_uuid = AsyncMock(side_effect=error) if error else AsyncMock(return_value=panel_user)

    @asynccontextmanager
    async def _client():
        yield api

    inst = MagicMock()
    inst.is_configured = True
    inst.get_api_client = _client
    return patch('app.services.subscription_service.SubscriptionService', return_value=inst), api


@asynccontextmanager
async def _drive(svc, user, deleted, panel_user=None, error=None):
    sub_patch, api = _panel_service(panel_user=panel_user, error=error)
    with (
        patch.object(type(rw.settings), 'is_multi_tariff_enabled', MagicMock(return_value=True)),
        sub_patch,
        patch.object(rw, 'decrement_subscription_server_counts', AsyncMock()),
    ):
        await svc._handle_user_deleted(AsyncMock(), user, deleted, {'uuid': deleted.remnawave_uuid})
        yield api


@pytest.mark.asyncio
async def test_pre_multitariff_sibling_with_future_end_date_not_expired():
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_uuid='DEAD', squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=900, remnawave_uuid=None, squads=['sq1', 'sq2'])
    user = SimpleNamespace(
        id=7, telegram_id=123, language='ru', remnawave_uuid='LIVE', subscriptions=[deleted, sibling]
    )

    svc = _service()
    async with _drive(svc, user, deleted, panel_user=None) as api:
        pass

    assert sibling.status == SubscriptionStatus.ACTIVE.value  # the reported bug — must stay active
    assert sibling.connected_squads == ['sq1', 'sq2']
    api.get_user_by_uuid.assert_not_awaited()  # future end_date short-circuits before any panel call


@pytest.mark.asyncio
async def test_sibling_alive_in_panel_via_user_uuid_fallback_not_expired():
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_uuid='DEAD', squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_uuid=None, squads=['sq1'])
    user = SimpleNamespace(
        id=7, telegram_id=123, language='ru', remnawave_uuid='LIVE', subscriptions=[deleted, sibling]
    )

    svc = _service()
    async with _drive(svc, user, deleted, panel_user=MagicMock()) as api:
        pass

    assert sibling.status == SubscriptionStatus.ACTIVE.value
    assert sibling.connected_squads == ['sq1']
    api.get_user_by_uuid.assert_awaited_with('LIVE')  # fell back to user.remnawave_uuid


@pytest.mark.asyncio
async def test_sibling_not_expired_on_transient_api_error():
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_uuid='DEAD', squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_uuid='SIB', squads=['sq1'])
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_uuid=None, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(svc, user, deleted, error=RuntimeError('panel down')):
        pass

    assert sibling.status == SubscriptionStatus.ACTIVE.value  # fail-safe: unknown != gone


@pytest.mark.asyncio
async def test_sibling_genuinely_gone_is_still_expired():
    """Don't break legitimate expiry: panel says gone (None) + past end_date -> expire."""
    deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_uuid='DEAD', squads=[])
    sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_uuid='SIB', squads=['sq1'])
    user = SimpleNamespace(id=7, telegram_id=123, language='ru', remnawave_uuid=None, subscriptions=[deleted, sibling])

    svc = _service()
    async with _drive(svc, user, deleted, panel_user=None):
        pass

    assert sibling.status == SubscriptionStatus.EXPIRED.value
    assert sibling.connected_squads == []


@pytest.mark.asyncio
async def test_intentional_panel_deletion_suppresses_sibling_sweep():
    svc = _service()  # resets class-level guards first
    rw.RemnaWaveWebhookService.mark_intentional_panel_deletion(panel_uuids=['DEAD'])
    try:
        deleted = _sub(1, SubscriptionStatus.EXPIRED.value, end_days=-10, remnawave_uuid='DEAD', squads=[])
        sibling = _sub(2, SubscriptionStatus.ACTIVE.value, end_days=-1, remnawave_uuid='SIB', squads=['sq1'])
        user = SimpleNamespace(
            id=7, telegram_id=123, language='ru', remnawave_uuid=None, subscriptions=[deleted, sibling]
        )

        await svc._handle_user_deleted(AsyncMock(), user, deleted, {'uuid': 'DEAD'})

        assert sibling.status == SubscriptionStatus.ACTIVE.value  # handler returned early, nothing swept
        svc._notify_user.assert_not_awaited()
    finally:
        rw.RemnaWaveWebhookService._intentional_panel_deletions_by_uuid.clear()
