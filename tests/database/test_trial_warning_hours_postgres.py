"""Exercise warning selection and durable deduplication on PostgreSQL."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, SentNotification, Subscription, User
from app.services import monitoring_service as module
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('hours', [2, 12, 24])
async def test_trial_window_boundaries_and_repeat_cycle(postgres_database, monkeypatch, hours):
    now = datetime(2026, 1, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module.settings, 'TRIAL_WARNING_HOURS', hours)
    monkeypatch.setattr(module.settings, 'ENABLE_NOTIFICATIONS', True)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        for ident in range(1, 8):
            db.add(User(id=ident, telegram_id=100000 + ident, status='blocked' if ident == 6 else 'active'))
        await db.flush()
        for ident, delta in enumerate([1, hours, hours + 0.01, 0, -1, 1, 1], 1):
            db.add(
                Subscription(
                    id=ident,
                    remnawave_short_id=f'masked-{ident}',
                    user_id=ident,
                    status='active',
                    is_trial=ident != 7,
                    start_date=now - timedelta(days=1),
                    end_date=now + timedelta(hours=delta),
                )
            )
        await db.commit()
        service = module.MonitoringService.__new__(module.MonitoringService)
        service.bot = object()
        service._send_trial_ending_notification = AsyncMock(return_value=True)
        service._log_monitoring_event = AsyncMock()
        await service._check_trial_expiring_soon(db)
        assert {call.args[1].id for call in service._send_trial_ending_notification.await_args_list} == {1, 2}
        records = (await observer.execute(select(SentNotification))).scalars().all()
        assert {row.subscription_id for row in records} == {1, 2}
        assert {row.notification_type for row in records} == {'trial_2h'}
        service._send_trial_ending_notification.reset_mock()
        await service._check_trial_expiring_soon(db)
        service._send_trial_ending_notification.assert_not_awaited()


@pytest.mark.parametrize('trigger_days', [30, 45, 60])
async def test_third_wave_reaches_configured_day_on_postgres(postgres_database, monkeypatch, trigger_days):
    now = datetime.now(UTC)
    monkeypatch.setattr(module.settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(module.settings, 'NOTIFICATION_EXPIRED_1D_ENABLED', False)
    monkeypatch.setattr(module.settings, 'NOTIFICATION_EXPIRED_WAVE2_ENABLED', False)
    monkeypatch.setattr(module.settings, 'NOTIFICATION_EXPIRED_WAVE3_ENABLED', True)
    monkeypatch.setattr(module.settings, 'NOTIFICATION_EXPIRED_WAVE3_TRIGGER_DAYS', trigger_days)
    monkeypatch.setattr(type(module.settings), 'is_multi_tariff_enabled', lambda self: False)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        db.add(User(id=1, telegram_id=100001, status='active'))
        await db.flush()
        db.add(
            Subscription(
                id=1,
                user_id=1,
                remnawave_short_id='masked-1',
                status='expired',
                is_trial=False,
                start_date=now - timedelta(days=90),
                end_date=now - timedelta(days=trigger_days, hours=12),
            )
        )
        await db.commit()
        service = module.MonitoringService.__new__(module.MonitoringService)
        service.bot = object()
        service._send_expired_discount_notification = AsyncMock(return_value=True)
        service._log_monitoring_event = AsyncMock()
        await service._check_expired_subscription_followups(db)
        service._send_expired_discount_notification.assert_awaited_once()
        assert service._send_expired_discount_notification.await_args.kwargs['trigger_days'] == trigger_days
        assert await observer.scalar(select(SentNotification.notification_type)) == 'expired_discount_wave3'
        service._send_expired_discount_notification.reset_mock()
        await service._check_expired_subscription_followups(db)
        service._send_expired_discount_notification.assert_not_awaited()
