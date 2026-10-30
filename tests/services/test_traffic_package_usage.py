"""The renewal policy must read the correct panel account without writing state."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.traffic_package_usage import read_panel_used_gb


@pytest.mark.asyncio
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('outcome', ['ok', 'missing', 'error', 'cancel'])
async def test_usage_reader_selects_account_and_preserves_transaction(monkeypatch, multi, outcome):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    sub = SimpleNamespace(id=1, user_id=2, remnawave_id='subscription-account', traffic_used_gb=40)
    monkeypatch.setattr(
        'app.services.traffic_package_usage.get_user_by_id',
        AsyncMock(return_value=SimpleNamespace(remnawave_id='owner-account')),
    )
    api = SimpleNamespace(
        get_user_by_id=AsyncMock(
            return_value=None if outcome == 'missing' else SimpleNamespace(used_traffic_bytes=188 * 1024**3)
        )
    )
    if outcome in ('error', 'cancel'):
        api.get_user_by_id.side_effect = RuntimeError('offline') if outcome == 'error' else asyncio.CancelledError()

    @asynccontextmanager
    async def client():
        yield api

    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService',
        lambda: SimpleNamespace(get_api_client=client, _bytes_to_gb=lambda value: value / 1024**3),
    )
    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await read_panel_used_gb(db, sub)
    else:
        assert await read_panel_used_gb(db, sub) == (188 if outcome == 'ok' else None)
    api.get_user_by_id.assert_awaited_once_with('subscription-account' if multi else 'owner-account')
    assert sub.traffic_used_gb == 40
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()
