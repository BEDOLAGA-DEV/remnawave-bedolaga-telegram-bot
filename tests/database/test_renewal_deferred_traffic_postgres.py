"""Renewal and daily expiry serialize on the same subscription row."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.models import Base, TrafficPurchase
from app.services.daily_subscription_service import DailySubscriptionService
from tests.crud.test_renewal_waits_for_panel_reset_before_dropping_topup import _seed
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_renewal_then_daily_cleanup_after_panel_reset(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr('app.services.traffic_package_usage.read_panel_used_gb', AsyncMock(return_value=188))
    monkeypatch.setattr('app.services.recurrent_amount.sync_recurrent_bindings_after_price_change', AsyncMock())
    panel = SimpleNamespace(
        update_remnawave_user=AsyncMock(return_value=SimpleNamespace(used_traffic_bytes=0)),
        _gb_to_bytes=lambda gb: int(gb * 1024**3),
    )
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', lambda: panel)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        renewal,
        worker,
        observer,
    ):
        sub = await _seed(renewal, used_gb=40)
        pid = (await renewal.scalars(select(TrafficPurchase.id))).one()
        await extend_subscription(renewal, sub, 30, traffic_limit_gb=100, reset_used_traffic=False, commit=False)
        assert sub.traffic_limit_gb == 1100
        service = DailySubscriptionService()
        service._get_panel_used_gb = AsyncMock(return_value=0)
        task = asyncio.create_task(service._reset_subscription_traffic(worker, sub.id, [pid]))
        try:
            assert await lock_waiter_appeared(observer, timeout=2)
            await renewal.commit()
            assert await asyncio.wait_for(task, 10) == 1
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await renewal.refresh(sub)
        assert sub.traffic_limit_gb == 100
        assert sub.purchased_traffic_gb == 0
        assert list((await renewal.scalars(select(TrafficPurchase))).all()) == []
        panel.update_remnawave_user.assert_awaited_once()
