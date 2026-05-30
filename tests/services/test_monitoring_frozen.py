from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.monitoring_service as ms
from app.services.monitoring_service import MonitoringService


@pytest.fixture
def service():
    svc = MonitoringService.__new__(MonitoringService)
    svc.bot = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_check_frozen_resumes_past_deadline(service, monkeypatch):
    now = datetime.now(UTC)
    sub = SimpleNamespace(id=1, user_id=10, frozen_at=now - timedelta(days=40),
                          frozen_until=now - timedelta(days=1), user=SimpleNamespace(id=10))
    result = MagicMock()
    result.scalars.return_value.all.return_value = [sub]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    resume = AsyncMock()
    monkeypatch.setattr(ms, 'freeze_service', SimpleNamespace(resume_subscription=resume))

    await service._check_frozen_subscriptions(db)

    resume.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_frozen_none_due(service, monkeypatch):
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    resume = AsyncMock()
    monkeypatch.setattr(ms, 'freeze_service', SimpleNamespace(resume_subscription=resume))

    await service._check_frozen_subscriptions(db)

    resume.assert_not_awaited()
