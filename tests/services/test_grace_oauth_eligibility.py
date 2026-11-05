"""TG-078: persisted OAuth/email accounts participate in grace discovery and grants."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import GraceAccessSessionModel, Subscription, Tariff, User
from app.services import grace_access_runtime as runtime
from app.services.grace_access_service import (
    GraceAccessMode,
    GracePanelSnapshot,
    GraceReason,
    GraceStartDecision,
)
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_grace_access_service import FakePanelGateway


@pytest.mark.parametrize('provider', ['vk', 'yandex', 'email'])
@pytest.mark.parametrize('reason', [GraceReason.EXPIRED, GraceReason.LIMITED])
@pytest.mark.parametrize('multi', [False, True])
async def test_oauth_grace_is_discovered_and_persisted_without_telegram(monkeypatch, provider, reason, multi):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_EXPIRED_SQUAD_UUID', '11111111-1111-1111-1111-111111111111')
    monkeypatch.setattr(settings, 'GRACE_ACCESS_LIMITED_SQUAD_UUID', '22222222-2222-2222-2222-222222222222')
    monkeypatch.setattr(settings, 'GRACE_ACCESS_RESET_TRAFFIC_ON_START', False)
    monkeypatch.setattr(settings, 'GRACE_ACCESS_CANDIDATE_LOOKBACK_MINUTES', 30)
    tables = (User.__table__, Subscription.__table__, Tariff.__table__, GraceAccessSessionModel.__table__)
    now = datetime.now(UTC)
    end = now - timedelta(minutes=1) if reason is GraceReason.EXPIRED else now + timedelta(days=10)
    panel_id = 501
    async with memory_session(monkeypatch, tables) as db:
        user = User(
            telegram_id=None,
            auth_type='email',
            email='reader@example.com',
            email_verified=True,
            status='active',
            balance_kopeks=0,
            remnawave_id=panel_id if not multi else None,
            vk_id=1001 if provider == 'vk' else None,
            yandex_id='oauth-example' if provider == 'yandex' else None,
        )
        db.add(user)
        await db.flush()
        sub = Subscription(
            user_id=user.id,
            is_trial=False,
            status=reason.value,
            start_date=now - timedelta(days=30),
            end_date=end,
            remnawave_id=panel_id if multi else None,
            traffic_limit_gb=100,
            traffic_used_gb=100 if reason is GraceReason.LIMITED else 0,
            device_limit=3,
            connected_squads=['33333333-3333-3333-3333-333333333333'],
            grace_candidate_at=now,
            grace_candidate_reason=reason.value,
        )
        db.add(sub)
        await db.commit()
        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(runtime, 'AsyncSessionLocal', factory)
        panel = FakePanelGateway(
            GracePanelSnapshot(
                remnawave_id=panel_id,
                status=reason.value.upper(),
                expire_at=end,
                traffic_limit_bytes=100 * 1024**3,
                used_traffic_bytes=(100 * 1024**3 if reason is GraceReason.LIMITED else 0),
                squad_uuids=tuple(sub.connected_squads),
            )
        )
        monkeypatch.setattr(runtime, 'RemnawaveGracePanelGateway', lambda **kwargs: panel)
        worker = runtime.GraceAccessRuntime()
        worker._mode = GraceAccessMode.ACTIVE

        assert (sub.id, reason) in await worker._recent_candidate_ids()
        result = await worker.consider_candidate(sub.id, reason, source='test')

        assert result is not None and result.decision is GraceStartDecision.STARTED
        async with factory() as observer:
            row = (await observer.execute(select(GraceAccessSessionModel))).scalar_one()
            assert row.subscription_id == sub.id
            assert row.state == 'active'
            assert row.remnawave_id == panel_id
        assert len(panel.applied_overlays) == 1
        assert await worker._recent_candidate_ids() == []
        assert user.telegram_id is None
