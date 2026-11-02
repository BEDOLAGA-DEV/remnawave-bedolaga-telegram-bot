"""The outer monitoring transaction must be released during independent channel checks."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.services import monitoring_service as monitoring
from tests.fixtures.postgres_db import real_asyncpg, require_postgres_dsn


pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]

STEPS = (
    '_process_autopayments',
    '_reconcile_platega_subscriptions',
    '_reconcile_lava_subscriptions',
    '_reconcile_cashera_subscriptions',
    '_check_expired_subscriptions',
    '_check_expiring_subscriptions',
    '_check_trial_expiring_soon',
    '_check_traffic_warnings',
    '_check_low_balance_alerts',
    '_retry_stuck_guest_purchases',
    '_cleanup_expired_refresh_tokens',
    '_cleanup_button_click_logs',
    '_cleanup_inactive_users',
    '_sync_with_remnawave',
)


async def test_channel_checks_do_not_leave_outer_connection_idle_in_transaction(monkeypatch):
    dsn = require_postgres_dsn()
    with real_asyncpg():
        engine = create_async_engine(dsn, pool_size=1, max_overflow=0, pool_pre_ping=True)
        observer = create_async_engine(dsn, pool_size=1, max_overflow=0)
    monkeypatch.setattr(monitoring, 'AsyncSessionLocal', async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', False)
    observed = {}

    async def reads(db, *args, **kwargs):
        observed['pid'] = await db.scalar(text('SELECT pg_backend_pid()'))
        return 0

    # Keep the orchestration and its SQLAlchemy session real. Domain stages stand in
    # for completed reads; no payment, panel or Telegram operations are performed.
    monkeypatch.setattr(monitoring, 'deactivate_expired_offers', reads)
    monkeypatch.setattr(monitoring, 'cleanup_expired_promo_offer_discounts', reads)
    monkeypatch.setattr(monitoring.promo_offer_service, 'cleanup_expired_test_access', reads)
    service = monitoring.MonitoringService.__new__(monitoring.MonitoringService)
    service.bot = None
    monkeypatch.setattr(service, '_cleanup_notification_cache', AsyncMock())
    monkeypatch.setattr(service, '_log_monitoring_event', AsyncMock())
    for name in STEPS:
        monkeypatch.setattr(service, name, reads)

    async def channels(db):
        async with observer.connect() as conn:
            observed['state_during_channels'] = await conn.scalar(
                text('SELECT state FROM pg_stat_activity WHERE pid = :pid'), {'pid': observed['pid']}
            )
            if observed['state_during_channels'] == 'idle in transaction':
                # Deterministically model the server terminating a transaction left
                # idle during getChatMember waits, without a timing-dependent sleep.
                await conn.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': observed['pid']})

    async def followups(db):
        observed['followups'] = await db.scalar(text('SELECT 1'))

    monkeypatch.setattr(service, '_check_trial_channel_subscriptions', channels)
    monkeypatch.setattr(service, '_check_expired_subscription_followups', followups)
    try:
        await service._monitoring_cycle()
        assert observed.get('followups') == 1, observed
        assert observed['state_during_channels'] == 'idle'
    finally:
        await engine.dispose()
        await observer.dispose()
