from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.remnawave_webhook_service as rws
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


def _sub(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=1, user_id=10, status='active',
        frozen_at=now, frozen_until=now, last_webhook_update_at=None,
        tariff=None, is_daily_paused=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def service():
    svc = RemnaWaveWebhookService(AsyncMock())
    svc._notify_user = AsyncMock()
    svc._get_subscription_keyboard = lambda user: None
    return svc


@pytest.fixture(autouse=True)
def _patch_module(monkeypatch):
    # Freeze never stamps last_webhook_update_at, so the echo-guard cannot fire.
    monkeypatch.setattr(rws, 'is_recently_updated_by_webhook', lambda sub: False)
    # sa_inspect on a SimpleNamespace would fail — stub it to expose .dict.tariff
    monkeypatch.setattr(
        rws, 'sa_inspect', lambda sub: SimpleNamespace(dict={'tariff': getattr(sub, 'tariff', None)})
    )


@pytest.mark.asyncio
async def test_user_disabled_skips_frozen_subscription(service, monkeypatch):
    # A user-initiated freeze disables the panel user, which makes RemnaWave
    # echo a user.disabled webhook. That echo must NOT deactivate the DB
    # subscription or notify the user — the freeze owns the DISABLED panel
    # state and keeps the DB subscription ACTIVE+frozen_at.
    deactivate = AsyncMock()
    monkeypatch.setattr(rws, 'deactivate_subscription', deactivate)

    db = AsyncMock()
    user = SimpleNamespace(id=10, language='ru', telegram_id=123, status='active')
    sub = _sub()

    await service._handle_user_disabled(db, user, sub, {})

    deactivate.assert_not_awaited()
    service._notify_user.assert_not_awaited()
    assert sub.status == 'active'


@pytest.mark.asyncio
async def test_user_disabled_still_deactivates_non_frozen(service, monkeypatch):
    # A genuine external/admin disable (no freeze in progress) must still
    # deactivate the subscription and notify the user.
    deactivate = AsyncMock()
    monkeypatch.setattr(rws, 'deactivate_subscription', deactivate)

    db = AsyncMock()
    user = SimpleNamespace(id=10, language='ru', telegram_id=123, status='active')
    sub = _sub(frozen_at=None, frozen_until=None)

    await service._handle_user_disabled(db, user, sub, {})

    deactivate.assert_awaited_once()
    service._notify_user.assert_awaited_once()
