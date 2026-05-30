from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.freeze_service as fs
from app.services.freeze_service import FreezeError, FreezeService


def _sub(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=1, user_id=10, status='active', is_trial=False,
        end_date=now + timedelta(days=20), created_at=now - timedelta(days=60),
        remnawave_uuid='uuid-main', frozen_at=None, frozen_until=None,
        freeze_days_used_year=0, freeze_year=None, last_freeze_at=None,
        tariff=SimpleNamespace(is_daily=False),
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def service():
    svc = FreezeService()
    svc._subscription_service = MagicMock()
    svc._subscription_service.disable_remnawave_user = AsyncMock(return_value=True)
    svc._subscription_service.enable_remnawave_user = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_max_days_per_year', classmethod(lambda cls: 30))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_min_subscription_age_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_cooldown_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_min_freeze_days', classmethod(lambda cls: 3))
    monkeypatch.setattr(fs.FreezeSettingsService, 'get_max_single_freeze_days', classmethod(lambda cls: 30))
    yield


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_freeze_happy(service):
    sub = _sub()
    db = _db()
    await service.freeze_subscription(db, sub, SimpleNamespace(id=10))
    assert sub.frozen_at is not None
    assert sub.frozen_until is not None
    service._subscription_service.disable_remnawave_user.assert_awaited_once_with('uuid-main')
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_freeze_rejects_trial(service):
    sub = _sub(is_trial=True)
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))
    service._subscription_service.disable_remnawave_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_freeze_rejects_daily(service):
    sub = _sub(tariff=SimpleNamespace(is_daily=True))
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_rejects_already_frozen(service):
    sub = _sub(frozen_at=datetime.now(UTC))
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_rejects_young_subscription(service):
    sub = _sub(created_at=datetime.now(UTC) - timedelta(days=2))
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_rejects_quota_exhausted(service):
    sub = _sub(freeze_year=datetime.now(UTC).year, freeze_days_used_year=30)
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))


@pytest.mark.asyncio
async def test_freeze_panel_failure_rolls_back(service):
    service._subscription_service.disable_remnawave_user = AsyncMock(return_value=False)
    sub = _sub()
    db = _db()
    with pytest.raises(FreezeError):
        await service.freeze_subscription(db, sub, SimpleNamespace(id=10))
    assert sub.frozen_at is None
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_resume_happy_extends_end_date(service):
    now = datetime.now(UTC)
    frozen_at = now - timedelta(days=5)
    sub = _sub(frozen_at=frozen_at, frozen_until=now + timedelta(days=25),
               end_date=now + timedelta(days=10), freeze_year=now.year, freeze_days_used_year=0)
    db = _db()
    old_end = sub.end_date
    await service.resume_subscription(db, sub, SimpleNamespace(id=10), reason='manual')
    assert sub.frozen_at is None
    assert sub.end_date > old_end
    assert sub.freeze_days_used_year >= 5
    service._subscription_service.enable_remnawave_user.assert_awaited_once_with('uuid-main')


@pytest.mark.asyncio
async def test_resume_capped_at_frozen_until(service):
    now = datetime.now(UTC)
    sub = _sub(frozen_at=now - timedelta(days=40), frozen_until=now - timedelta(days=10),
               end_date=now, freeze_year=now.year)
    db = _db()
    old_end = sub.end_date
    await service.resume_subscription(db, sub, SimpleNamespace(id=10), reason='auto')
    delta_days = (sub.end_date - old_end).days
    assert delta_days <= 30


@pytest.mark.asyncio
async def test_resume_panel_failure_keeps_time_enqueues(service, monkeypatch):
    service._subscription_service.enable_remnawave_user = AsyncMock(return_value=False)
    enqueue = MagicMock()
    monkeypatch.setattr(fs.remnawave_retry_queue, 'enqueue', enqueue)
    now = datetime.now(UTC)
    sub = _sub(frozen_at=now - timedelta(days=5), frozen_until=now + timedelta(days=25),
               end_date=now + timedelta(days=10), freeze_year=now.year)
    db = _db()
    await service.resume_subscription(db, sub, SimpleNamespace(id=10), reason='manual')
    assert sub.frozen_at is None
    enqueue.assert_called_once()
