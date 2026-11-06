"""Managed quota delivery must preserve explicit metadata synchronization masks."""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import Subscription
from app.services.panel_sync.writer import push_subscription
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_delivery import deliver_account_projection
from app.services.traffic_accounting_service import lock_accounting_subscription
from app.services.traffic_accounting_store import initialize_account
from tests.services.test_traffic_accounting_service import END, NOW, PanelDouble
from tests.services.test_traffic_accounting_store import database


async def prepare(db):
    sub = await db.get(Subscription, 1)
    sub.remnawave_id = 123
    sub.traffic_limit_gb = 1
    sub.traffic_accounting_mode = 'managed'
    sub.status = 'active'
    sub.end_date = END
    state = await initialize_account(
        db,
        1,
        TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('base:old', 70, END),)),
        strategy='NO_RESET',
        timezone='UTC',
        anchor=NOW,
        next_refresh_at=None,
    )
    state.phase = 'ready'
    await db.commit()
    api = PanelDouble()
    api.user.status = 'ACTIVE'
    api.user.expire_at = END - timedelta(days=1)
    return api, async_sessionmaker(db.bind, expire_on_commit=False)


async def test_polling_does_not_rewrite_metadata(monkeypatch):
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert set(api.writes[-1]) == {'user_id', 'traffic_limit_bytes', 'traffic_limit_strategy'}


async def test_narrow_mask_and_custom_tag_survive_lost_reply(monkeypatch):
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        sub, _ = await lock_accounting_subscription(db, 1)
        await push_subscription(
            api, sub.user, sub, db=db, only_fields={'description', 'tag'}, user_tag='CUSTOM', now=NOW
        )
        await db.commit()
        api.fail_once = True
        with pytest.raises(TimeoutError):
            await deliver_account_projection(factory, api, 1, now=NOW)
        await deliver_account_projection(factory, api, 1, now=NOW)
        for write in api.writes:
            assert 'traffic_limit_bytes' not in write
            assert 'traffic_limit_strategy' not in write
            assert 'expire_at' not in write
            assert 'active_internal_squads' not in write
            assert 'hwid_device_limit' not in write
            assert 'status' not in write
            assert write['tag'] == 'CUSTOM'
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert 'tag' not in api.writes[-1]


async def test_full_sync_stages_expiry_until_delivery(monkeypatch):
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        sub, _ = await lock_accounting_subscription(db, 1)
        await push_subscription(api, sub.user, sub, db=db, now=NOW)
        await db.commit()
        api.fail_once = True
        with pytest.raises(TimeoutError):
            await deliver_account_projection(factory, api, 1, now=NOW)
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert all(write['expire_at'] == END for write in api.writes)
        count = len(api.writes)
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert len(api.writes) == count


async def test_pending_paid_expiry_is_delivered_without_a_service_retry(monkeypatch):
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        sub = await db.get(Subscription, 1)
        sub.pending_panel_expire_at = END
        await db.commit()
        original = api.update_user

        async def update(**kwargs):
            if 'expire_at' in kwargs:
                api.user.expire_at = kwargs['expire_at']
            return await original(**kwargs)

        api.update_user = update
        api.fail_once = True
        with pytest.raises(TimeoutError):
            await deliver_account_projection(factory, api, 1, now=NOW)
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert api.writes[-1]['expire_at'] == END
        count = len(api.writes)
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert len(api.writes) == count


async def test_narrow_request_cannot_discard_previous_undelivered_renewal(monkeypatch):
    from app.services.traffic_accounting_sync import stage_managed_sync

    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        sub, _ = await lock_accounting_subscription(db, 1)
        await stage_managed_sync(db, sub, only_fields={'expire_at'})
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        await push_subscription(api, sub.user, sub, db=db, only_fields={'tag'}, user_tag='CUSTOM', now=NOW)
        await db.commit()
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert api.writes[-1]['expire_at'] == END
        assert api.writes[-1]['tag'] == 'CUSTOM'
        assert 'active_internal_squads' not in api.writes[-1]


async def test_narrow_sync_does_not_recreate_when_disallowed(monkeypatch):
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    async with database(monkeypatch) as db:
        api, _ = await prepare(db)
        sub, _ = await lock_accounting_subscription(db, 1)
        api.user = None
        result = await push_subscription(
            api,
            sub.user,
            sub,
            db=db,
            create_if_missing=False,
            only_fields={'description'},
            now=NOW,
        )
        assert result.action == 'no_changes'
        assert api.writes == []


@pytest.mark.parametrize('reset_devices', [False, True, None])
async def test_device_reset_is_explicit_and_not_replayed_by_worker(monkeypatch, reset_devices):
    from unittest.mock import AsyncMock

    from app.config import settings

    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', True)
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        api.reset_user_devices = AsyncMock(return_value=True)
        sub, _ = await lock_accounting_subscription(db, 1)
        await push_subscription(api, sub.user, sub, db=db, reset_devices=reset_devices, now=NOW)
        assert api.reset_user_devices.await_count == (0 if reset_devices is False else 1)
        await db.commit()
        await deliver_account_projection(factory, api, 1, now=NOW)
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert api.reset_user_devices.await_count == (0 if reset_devices is False else 1)


async def test_staging_refreshes_a_cached_outbox_before_merging_fields(monkeypatch):
    from app.database.models import TrafficAccountingState
    from app.services.traffic_accounting_sync import stage_managed_sync

    async with database(monkeypatch) as db:
        _, factory = await prepare(db)
        cached = await db.get(TrafficAccountingState, 1)
        assert not cached.pending_sync_fields
        async with factory() as other:
            state = await other.get(TrafficAccountingState, 1)
            state.pending_sync_fields = ['expire_at']
            state.revision += 1
            await other.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        await stage_managed_sync(db, sub, only_fields={'tag'}, user_tag='CUSTOM')
        assert cached.pending_sync_fields == ['expire_at', 'tag']
        assert cached.revision == 2


async def test_unchanged_panel_echo_never_causes_another_patch(monkeypatch):
    """PATCH -> user.modified -> delivery must converge after the first write."""
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        await deliver_account_projection(factory, api, 1, now=NOW)
        initial_writes = len(api.writes)
        assert initial_writes == 1
        for _ in range(5):
            await deliver_account_projection(factory, api, 1, now=NOW)
        assert len(api.writes) == initial_writes


async def test_echo_still_repairs_real_quota_drift(monkeypatch):
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)
        await deliver_account_projection(factory, api, 1, now=NOW)
        api.user.traffic_limit_bytes += 10
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert len(api.writes) == 2
        assert api.user.traffic_limit_bytes == 100
        await deliver_account_projection(factory, api, 1, now=NOW)
        assert len(api.writes) == 2


async def test_modified_webhook_echo_converges(monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import datetime

    from app.services import remnawave_webhook_service as webhook, traffic_accounting_service as accounting
    from app.services.subscription_service import SubscriptionService

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    queued = []
    original_reconcile = accounting.reconcile_account_locked

    async def reconcile(db, subscription, panel, **kwargs):
        kwargs.setdefault('now', NOW)
        return await original_reconcile(db, subscription, panel, **kwargs)

    monkeypatch.setattr(webhook, 'datetime', Clock)
    monkeypatch.setattr(accounting, 'reconcile_account_locked', reconcile)
    monkeypatch.setattr(accounting, 'schedule_projection_after_commit', lambda db, target: queued.append(target))
    async with database(monkeypatch) as db:
        api, factory = await prepare(db)

        @asynccontextmanager
        async def client(self):
            yield api

        monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
        await deliver_account_projection(factory, api, 1, now=NOW)
        count = len(api.writes)
        handler = webhook.RemnaWaveWebhookService(bot=None)
        await handler._reconcile_managed_status_event(db, await db.get(Subscription, 1))
        assert queued == [1]
        for target in queued:
            await deliver_account_projection(factory, api, target, now=NOW)
        assert len(api.writes) == count
