"""Unit tests for /subscription/nodes-latency-targets endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def fake_user():
    u = SimpleNamespace()
    u.id = 1
    return u


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_speedtest_disabled_returns_404(fake_user, mock_db):
    from app.cabinet.routes.subscription_modules import speedtest as st

    with patch.object(st.settings, 'SPEEDTEST_ENABLED', False):
        with pytest.raises(HTTPException) as exc:
            await st.nodes_latency_targets(user=fake_user, db=mock_db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_speedtest_no_subscription_returns_403(fake_user, mock_db):
    from app.cabinet.routes.subscription_modules import speedtest as st

    with (
        patch.object(st.settings, 'SPEEDTEST_ENABLED', True),
        patch.object(st, 'get_active_subscriptions_by_user_id', AsyncMock(return_value=[])),
    ):
        with pytest.raises(HTTPException) as exc:
            await st.nodes_latency_targets(user=fake_user, db=mock_db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_speedtest_happy_path_returns_targets_and_samples(fake_user, mock_db):
    from app.config import Settings
    from app.cabinet.routes.subscription_modules import speedtest as st

    sample_targets = [{'host': '1.2.3.4', 'port': 443, 'label': 'Node 1'}]

    with (
        patch.object(st.settings, 'SPEEDTEST_ENABLED', True),
        patch.object(st, 'get_active_subscriptions_by_user_id', AsyncMock(return_value=[object()])),
        patch.object(st.speedtest_service, 'get_ping_targets', AsyncMock(return_value=sample_targets)),
        patch.object(Settings, 'get_speedtest_samples', return_value=5),
    ):
        result = await st.nodes_latency_targets(user=fake_user, db=mock_db)

    assert 'targets' in result
    assert isinstance(result['targets'], list)
    assert result['targets'] == sample_targets
    assert 'samples' in result
    assert isinstance(result['samples'], int)
    assert result['samples'] == 5
