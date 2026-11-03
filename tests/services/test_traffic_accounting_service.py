"""Runtime accounting uses current counters, never grants on a panel reset."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.traffic_accounting import AccountingConflict, Allowance, TrafficAccount


NOW = datetime(2026, 1, 15, tzinfo=UTC)
END = NOW + timedelta(days=30)


def snapshot(*, used=30, lifetime=100, strategy='MONTH'):
    return SimpleNamespace(
        id=123,
        created_at=NOW - timedelta(days=60),
        user_traffic=SimpleNamespace(used_traffic_bytes=used, lifetime_used_traffic_bytes=lifetime),
        traffic_limit_bytes=100,
        traffic_limit_strategy=strategy,
    )


def test_missing_user_traffic_is_not_zero():
    from app.services.traffic_accounting_service import read_counters

    panel = snapshot()
    panel.user_traffic = None
    with pytest.raises(AccountingConflict, match='snapshot'):
        read_counters(panel)


def test_identity_includes_account_creation_epoch():
    from app.services.traffic_accounting_service import read_counters

    panel = snapshot()
    first = read_counters(panel)
    panel.created_at += timedelta(seconds=1)
    assert read_counters(panel).identity != first.identity


def test_fresh_reset_does_not_regrant_package():
    from app.services.traffic_accounting_service import settle_snapshot

    panel = snapshot()
    account = TrafficAccount('123@2025-11-16T00:00:00+00:00', 70, 0, (Allowance('purchase:1', 100, END),))
    updated = settle_snapshot(account, panel, now=NOW)
    assert updated.remaining_bytes(NOW) == 70
    panel.user_traffic.used_traffic_bytes = 0
    assert settle_snapshot(updated, panel, now=NOW).remaining_bytes(NOW) == 70


def test_grace_usage_does_not_debit_paid_remainder():
    from app.services.traffic_accounting_service import settle_snapshot

    account = TrafficAccount('123@2025-11-16T00:00:00+00:00', 70, 0, (Allowance('purchase:1', 100, END),))
    updated = settle_snapshot(account, snapshot(), now=NOW, exclude_usage=True)
    assert updated.remaining_bytes(NOW) == 100
    assert updated.lifetime_bytes == 100


def test_grace_does_not_hide_changed_identity():
    from app.services.traffic_accounting_service import settle_snapshot

    account = TrafficAccount('another', 70, 0, (Allowance('purchase:1', 100, END),))
    with pytest.raises(AccountingConflict):
        settle_snapshot(account, snapshot(), now=NOW, exclude_usage=True)


async def test_summary_never_labels_nominal_legacy_packages_as_remaining(monkeypatch):
    from app.services.traffic_accounting_service import traffic_accounting_summary
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        summary = await traffic_accounting_summary(db, 1, now=NOW)
        assert summary['traffic_accounting_mode'] == 'legacy'
        assert summary['traffic_remaining_gb'] is None
        assert summary['purchase_remaining'] == {}


async def test_summary_returns_per_package_remainders_and_ignores_expired_grants(monkeypatch):
    from app.services.traffic_accounting_service import traffic_accounting_summary
    from app.services.traffic_accounting_store import initialize_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        from app.database.models import Subscription

        (await db.get(Subscription, 1)).traffic_limit_gb = 100
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                '123',
                100,
                30,
                (
                    Allowance('base:jan', 2 * 1024**3, END),
                    Allowance('purchase:17', 1024**3, END),
                    Allowance('purchase:18', 1024**3, NOW),
                ),
            ),
            strategy='MONTH',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=END,
        )
        state.phase = 'ready'
        await db.flush()
        summary = await traffic_accounting_summary(db, 1, now=NOW)
        assert summary['traffic_accounting_mode'] == 'managed'
        assert summary['traffic_remaining_gb'] == 3
        assert summary['base_traffic_remaining_gb'] == 2
        assert summary['purchase_remaining'] == {17: 1.0, 18: 0.0}


class PanelDouble:
    def __init__(self):
        self.user = snapshot()
        self.fail_once = False
        self.writes = []

    async def get_user_devices_all(self, user_id):
        return {'devices': [], 'total': 0}

    async def get_user_by_id(self, user_id):
        assert user_id == 123
        return self.user

    async def update_user(self, **kwargs):
        self.writes.append(kwargs)
        self.user.traffic_limit_strategy = kwargs.get('traffic_limit_strategy', self.user.traffic_limit_strategy)
        self.user.traffic_limit_bytes = kwargs.get('traffic_limit_bytes', self.user.traffic_limit_bytes)
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError('response lost')
        return self.user


async def test_migration_timeout_is_resumable_without_compensation(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database.models import Subscription, TrafficAccountingState
    from app.services.traffic_accounting_service import ensure_managed_account, traffic_accounting_summary
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.remnawave_id = 123
        sub.traffic_limit_gb = 1
        await db.commit()
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        panel = PanelDouble()
        panel.fail_once = True
        with pytest.raises(TimeoutError):
            await ensure_managed_account(maker, panel, 1, now=NOW, timezone='UTC')
        await db.rollback()
        state = await db.get(TrafficAccountingState, 1)
        assert state.phase == 'prepared'
        assert panel.user.traffic_limit_strategy.value == 'NO_RESET'
        await db.rollback()
        await ensure_managed_account(maker, panel, 1, now=NOW, timezone='UTC')
        summary = await traffic_accounting_summary(db, 1, now=NOW)
        assert summary['traffic_remaining_gb'] == 70 / 1024**3
        assert summary['traffic_accounting_mode'] == 'managed'
        assert panel.user.traffic_limit_bytes == 100


async def test_migration_blocks_pending_paid_reset_without_writing_panel(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database.models import PaidTrafficResetIntent, Subscription
    from app.services.traffic_accounting_service import ensure_managed_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.remnawave_id = 123
        sub.traffic_limit_gb = 1
        db.add(
            PaidTrafficResetIntent(
                subscription_id=1, user_id=1, operation_key='daily:1', charge_at=NOW, reason='daily', state='pending'
            )
        )
        await db.commit()
        panel = PanelDouble()
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        with pytest.raises(AccountingConflict, match='reset'):
            await ensure_managed_account(maker, panel, 1, now=NOW, timezone='UTC')
        assert panel.writes == []


async def test_purchase_settles_old_usage_before_issuing_new_grant(monkeypatch):
    from app.database.models import Subscription
    from app.services.traffic_accounting_service import grant_purchase_locked, lock_accounting_subscription
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        panel = snapshot(used=100, lifetime=100, strategy='NO_RESET')
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 0, 0, (Allowance('purchase:old', 50, END),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        purchase = SimpleNamespace(id=12, traffic_gb=1, expires_at=END, subscription_id=1)
        account = await grant_purchase_locked(db, sub, purchase, panel, now=NOW)
        assert account.remaining_bytes(NOW) == 1024**3
        await db.rollback()
        account, _ = await load_account(db, 1)
        assert all(item.key != 'purchase:12' for item in account.allowances)


async def test_calendar_refresh_never_regrants_purchased_traffic(monkeypatch):
    from app.database.models import Subscription
    from app.services.traffic_accounting_service import lock_accounting_subscription, reconcile_account_locked
    from app.services.traffic_accounting_store import initialize_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        panel = snapshot(used=100, lifetime=100, strategy='NO_RESET')
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 100, (Allowance('purchase:1', 0, END, 1024**3),)),
            strategy='MONTH',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=NOW,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.traffic_limit_gb = 2
        sub.purchased_traffic_gb = 1
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        first = await reconcile_account_locked(db, sub, panel, now=NOW)
        second = await reconcile_account_locked(db, sub, panel, now=NOW)
        assert {a.key: a for a in first.allowances} == {a.key: a for a in second.allowances}
        assert first.remaining_bytes(NOW) == 1024**3
        assert first.allowances[0].remaining_bytes == 0


async def test_central_writer_rebuilds_managed_quota_from_latest_ledger(monkeypatch):
    from app.database.models import Subscription
    from app.services.panel_sync.writer import push_subscription
    from app.services.traffic_accounting_store import initialize_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('purchase:1', 20, END),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.remnawave_id = 123
        sub.traffic_limit_gb = 50
        await db.commit()
        panel = PanelDouble()
        from app.services import traffic_accounting_service

        monkeypatch.setattr(traffic_accounting_service, 'schedule_projection_after_commit', lambda *args: None)
        result = await push_subscription(
            panel, SimpleNamespace(id=1), sub, db=db, now=NOW, reset_devices=False, only_fields={'description'}
        )
        assert result.action == 'queued'
        assert panel.writes == []
        await db.commit()
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.services.traffic_accounting_delivery import deliver_account_projection

        await deliver_account_projection(
            async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False), panel, 1, now=NOW
        )
        assert set(panel.writes[-1]) == {'user_id', 'description'}
        # A later quota poll is independent of the administrator's field mask.
        await deliver_account_projection(
            async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False), panel, 1, now=NOW
        )
        assert panel.writes[-1]['traffic_limit_bytes'] == 50
        assert panel.writes[-1]['traffic_limit_strategy'].value == 'NO_RESET'
        assert sub.traffic_limit_gb == 50


async def test_unlimited_transition_preserves_history_but_cannot_regrant_packages(monkeypatch):
    from app.database.models import Subscription
    from app.services.traffic_accounting_service import lock_accounting_subscription, transition_to_unlimited_locked
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('purchase:1', 20, END),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        await transition_to_unlimited_locked(db, sub, snapshot(), now=NOW)
        account, state = await load_account(db, 1)
        assert state.phase == 'unlimited'
        assert account.allowances[0].granted_bytes == 20
        assert account.allowances[0].remaining_bytes == 0
        assert sub.traffic_limit_gb == 0
        from app.services.panel_sync.writer import push_subscription

        sub.remnawave_id = 123
        await db.flush()
        panel = PanelDouble()
        from app.services import traffic_accounting_service

        monkeypatch.setattr(traffic_accounting_service, 'schedule_projection_after_commit', lambda *args: None)
        await push_subscription(panel, sub.user, sub, db=db, now=NOW, reset_devices=False)
        await db.commit()
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.services.traffic_accounting_delivery import deliver_account_projection

        await deliver_account_projection(
            async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False), panel, 1, now=NOW
        )
        assert panel.writes[-1]['traffic_limit_bytes'] == 0
        assert panel.writes[-1]['traffic_limit_strategy'].value == 'NO_RESET'


async def test_subscription_service_managed_update_never_physically_resets(monkeypatch):
    from contextlib import asynccontextmanager

    from app.database.models import Subscription
    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('purchase:1', 20, END),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.remnawave_id = 123
        sub.traffic_limit_gb = 2
        sub.purchased_traffic_gb = 1
        await db.commit()
        panel = PanelDouble()

        @asynccontextmanager
        async def client():
            yield panel

        service = SubscriptionService()
        monkeypatch.setattr(service, 'get_api_client', client)
        result = await service.update_remnawave_user(db, sub, reset_traffic=True, reset_reason='renewal')
        assert result is panel.user
        account, _ = await load_account(db, 1)
        assert not any(item.key.startswith('base:renewal:') for item in account.allowances)
        assert next(item.remaining_bytes for item in account.allowances if item.key == 'purchase:1') == 0


async def test_tariff_resize_preserves_consumed_base_and_retires_package_history(monkeypatch):
    from app.services.traffic_accounting_service import change_managed_tariff_locked, lock_accounting_subscription
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                '123@2025-11-16T00:00:00+00:00',
                100,
                30,
                (Allowance('base:old', 2 * 1024**3, END, 5 * 1024**3), Allowance('purchase:1', 20, END)),
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        state.base_budget_bytes = 5 * 1024**3
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        sub.traffic_limit_gb = 4
        sub.purchased_traffic_gb = 0
        await change_managed_tariff_locked(db, sub, snapshot(), operation_key='admin:17', reset_base=False, now=NOW)
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == 1024**3
        assert next(a for a in account.allowances if a.key == 'purchase:1').remaining_bytes == 0


async def test_grace_lifecycle_excludes_overlay_bytes_and_resumes_paid_meter(monkeypatch):
    from contextlib import asynccontextmanager

    from app.database.models import Subscription
    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_service import finish_grace_accounting, prepare_grace_accounting
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('purchase:1', 100, END),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.remnawave_id = 123
        await db.commit()
        panel = PanelDouble()

        @asynccontextmanager
        async def client(self):
            yield panel

        monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
        from app.services import traffic_accounting_service

        monkeypatch.setattr(traffic_accounting_service, 'schedule_projection_after_commit', lambda *args: None)
        await prepare_grace_accounting(db, 1, now=NOW)
        await db.commit()
        panel.user.user_traffic.lifetime_used_traffic_bytes += 70
        panel.user.user_traffic.used_traffic_bytes += 70
        await finish_grace_accounting(db, 1, now=NOW)
        await db.commit()
        account, state = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == 100
        assert state.grace_excluded is False
        panel.user.user_traffic.lifetime_used_traffic_bytes += 20
        panel.user.user_traffic.used_traffic_bytes += 20
        from app.services.traffic_accounting_service import lock_accounting_subscription, reconcile_account_locked

        sub, _ = await lock_accounting_subscription(db, 1)
        account = await reconcile_account_locked(db, sub, panel.user, now=NOW)
        assert account.remaining_bytes(NOW) == 80


async def test_lost_disable_response_keeps_durable_intent_and_purchase_can_enable(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database.models import Subscription, TrafficAccountingState
    from app.services.traffic_accounting_delivery import deliver_account_projection
    from app.services.traffic_accounting_service import grant_purchase_locked, lock_accounting_subscription
    from app.services.traffic_accounting_store import initialize_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 0, 0, ()),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.remnawave_id = 123
        sub.traffic_limit_gb = 1
        sub.status = 'active'
        await db.commit()
        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        panel = PanelDouble()
        panel.user = snapshot(used=0, lifetime=0, strategy='NO_RESET')
        panel.fail_once = True
        with pytest.raises(TimeoutError):
            await deliver_account_projection(factory, panel, 1, now=NOW)
        await db.rollback()
        state = await db.get(TrafficAccountingState, 1, populate_existing=True)
        assert state.quota_disabled is True
        assert state.projected_revision < state.revision
        panel.user.status = 'DISABLED'
        sub, _ = await lock_accounting_subscription(db, 1)
        await grant_purchase_locked(
            db, sub, SimpleNamespace(id=23, subscription_id=1, traffic_gb=1, expires_at=END), panel.user, now=NOW
        )
        await db.commit()
        await deliver_account_projection(factory, panel, 1, now=NOW)
        assert panel.writes[-1]['status'].value == 'ACTIVE'
        assert panel.writes[-1]['traffic_limit_bytes'] == 1024**3


async def test_unlimited_to_finite_restores_calendar_without_resurrecting_old_packages(monkeypatch):
    from app.database.models import Subscription
    from app.services.traffic_accounting_service import (
        change_managed_tariff_locked,
        lock_accounting_subscription,
        transition_to_unlimited_locked,
    )
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('purchase:1', 100, END),)),
            strategy='MONTH',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=END,
        )
        state.phase = 'ready'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        await transition_to_unlimited_locked(db, sub, snapshot(), now=NOW)
        sub.traffic_limit_gb = 4
        await change_managed_tariff_locked(db, sub, snapshot(), operation_key='switch:19', reset_base=False, now=NOW)
        account, state = await load_account(db, 1)
        assert state.phase == 'ready'
        assert state.next_refresh_at == datetime(2026, 2, 1, 0, 20, tzinfo=UTC)
        assert account.remaining_bytes(NOW) == 4 * 1024**3
        assert next(a for a in account.allowances if a.key == 'purchase:1').remaining_bytes == 0


async def test_physical_reset_guard_reads_persisted_accounting_fence(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_store import initialize_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, ()),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        await db.commit()
        panel = SimpleNamespace(reset_user_traffic=AsyncMock())
        with pytest.raises(AccountingConflict, match='physical reset'):
            await SubscriptionService()._reset_user_traffic(panel, 123, SimpleNamespace(id=1), db=db)
        panel.reset_user_traffic.assert_not_awaited()


async def test_prepared_migration_resumes_after_calendar_boundary_preserving_available(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database.models import Subscription, TrafficAccountingState
    from app.services.traffic_accounting_service import ensure_managed_account, traffic_accounting_summary
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.remnawave_id = 123
        sub.traffic_limit_gb = 1
        await db.commit()
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        panel = PanelDouble()
        panel.fail_once = True
        with pytest.raises(TimeoutError):
            await ensure_managed_account(maker, panel, 1, now=NOW)
        later = NOW + timedelta(days=60)
        await ensure_managed_account(maker, panel, 1, now=later)
        summary = await traffic_accounting_summary(db, 1, now=later)
        assert summary['traffic_remaining_gb'] == 70 / 1024**3
        state = await db.get(TrafficAccountingState, 1)
        assert state.next_refresh_at > later


async def test_migration_does_not_promote_expired_packages_into_calendar_base(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database.models import Subscription, TrafficAccountingState, TrafficPurchase
    from app.services.traffic_accounting_service import ensure_managed_account, traffic_accounting_summary
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        sub = await db.get(Subscription, 1)
        sub.remnawave_id = 123
        sub.traffic_limit_gb = 100
        sub.purchased_traffic_gb = 60
        db.add(TrafficPurchase(subscription_id=1, traffic_gb=40, expires_at=END))
        db.add(TrafficPurchase(subscription_id=1, traffic_gb=20, expires_at=NOW - timedelta(days=1)))
        await db.commit()
        panel = PanelDouble()
        panel.user.traffic_limit_bytes = 100 * 1024**3
        panel.user.user_traffic.used_traffic_bytes = 20 * 1024**3
        panel.user.user_traffic.lifetime_used_traffic_bytes = 20 * 1024**3
        await ensure_managed_account(
            async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False), panel, 1, now=NOW
        )
        state = await db.get(TrafficAccountingState, 1)
        assert state.base_budget_bytes == 40 * 1024**3
        summary = await traffic_accounting_summary(db, 1, now=NOW)
        assert summary['traffic_remaining_gb'] == 80
        assert summary['base_traffic_remaining_gb'] == 40


async def test_missing_account_during_grace_does_not_prevent_closing_exclusion(monkeypatch):
    from contextlib import asynccontextmanager

    from app.database.models import Subscription
    from app.services import traffic_accounting_service as service
    from app.services.subscription_service import SubscriptionService
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('purchase:1', 70, END),)),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        state.grace_excluded = True
        sub = await db.get(Subscription, 1)
        sub.remnawave_id = 123
        sub.traffic_accounting_mode = 'managed'
        await db.commit()
        panel = PanelDouble()
        panel.user = None

        @asynccontextmanager
        async def client(self):
            yield panel

        monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
        monkeypatch.setattr(service, 'schedule_projection_after_commit', lambda *args: None)
        await service.prepare_grace_accounting(db, 1, now=NOW)
        await service.finish_grace_accounting(db, 1, now=NOW)
        account, state = await load_account(db, 1)
        assert state.grace_excluded is False
        assert account.remaining_bytes(NOW) == 70


async def test_reconcile_consumes_paid_strategy_intent_before_old_calendar(monkeypatch):
    from app.database.models import Subscription, TrafficBaseGrantIntent
    from app.services.traffic_accounting_service import lock_accounting_subscription, reconcile_account_locked
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    async with database(monkeypatch) as db:
        state = await initialize_account(
            db,
            1,
            TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, ()),
            strategy='DAY',
            timezone='UTC',
            anchor=NOW,
            next_refresh_at=NOW - timedelta(seconds=1),
        )
        state.phase = 'ready'
        state.last_base_operation_at = NOW - timedelta(days=1)
        sub = await db.get(Subscription, 1)
        sub.traffic_limit_gb = 2
        sub.traffic_accounting_mode = 'managed'
        db.add(
            TrafficBaseGrantIntent(
                subscription_id=1,
                operation_key='renewal:strategy',
                base_bytes=2 * 1024**3,
                target_unlimited=False,
                reset_base=True,
                business_strategy='MONTH',
                tariff_id=None,
                requested_at=NOW - timedelta(seconds=10),
                expires_at=datetime(2026, 2, 1, 0, 20, tzinfo=UTC),
                state='pending',
            )
        )
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        await reconcile_account_locked(db, sub, snapshot(), now=NOW)
        account, state = await load_account(db, 1)
        assert state.business_strategy == 'MONTH'
        assert account.remaining_bytes(NOW) == 2 * 1024**3
