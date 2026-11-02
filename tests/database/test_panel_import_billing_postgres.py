"""Panel imports preserve exact identities and do not invent billing choices."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, Tariff, TrafficPurchase, Transaction, User
from app.services.remnawave_service import RemnaWaveService
from tests.fixtures.postgres_db import postgres_session, postgres_sessions, wait_for_lock_waiter


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)
NOW = datetime.now(UTC)
END = NOW + timedelta(days=30)
GIB = 1024**3


def account(*, tag=None, panel_id=9001, short='shared-short', username='account_local10'):
    return SimpleNamespace(
        id=panel_id,
        short_uuid=short,
        username=username,
        status=SimpleNamespace(value='ACTIVE'),
        telegram_id=101,
        email=None,
        expire_at=END,
        used_traffic_bytes=5 * GIB,
        traffic_limit_bytes=120 * GIB,
        hwid_device_limit=5,
        subscription_url=f'https://subscription.example/{short}',
        happ_crypto_link='',
        active_internal_squads=[{'uuid': 'test-squad'}],
        tag=tag,
    )


def service(monkeypatch, accounts, *, multi=True):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'TRIAL_USER_TAG', 'TRIAL')
    api = SimpleNamespace(
        get_all_users_page_stream=AsyncMock(return_value={'users': accounts, 'hasMore': False, 'nextCursor': None}),
        get_user_by_id=AsyncMock(side_effect=lambda panel_id: next((a for a in accounts if a.id == panel_id), None)),
    )

    @asynccontextmanager
    async def client(self):
        yield api

    svc = RemnaWaveService()
    monkeypatch.setattr(svc, 'get_api_client', lambda: client(svc))
    svc.test_api = api
    return svc


def tariff(tariff_id=1, *, daily=False):
    return Tariff(
        id=tariff_id,
        name=f'Test {tariff_id}',
        is_active=True,
        is_daily=daily,
        daily_price_kopeks=1000 if daily else 0,
        allowed_squads=['test-squad'],
        traffic_limit_gb=100,
        device_limit=2,
        period_prices={'30': 3000},
    )


async def seed(db, *, user_panel_id=None):
    user = User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=user_panel_id)
    db.add(user)
    await db.commit()
    return user


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize(
    ('configured_tag', 'panel_tag', 'trial'),
    [
        ('TRIAL', 'TRIAL', True),
        ('TRY', 'TRY', True),
        (None, 'TRIAL', True),
        (None, None, False),
        ('TRIAL', 'PAID', False),
        ('TRIAL', None, False),
        ('TRY', 'TRIAL', False),
    ],
)
async def test_import_reads_configured_trial_marker(
    postgres_database, monkeypatch, multi, configured_tag, panel_tag, trial
):
    svc = service(monkeypatch, [account(tag=panel_tag)], multi=multi)
    monkeypatch.setattr(settings, 'TRIAL_USER_TAG', configured_tag)
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db)
        stats = await svc.sync_users_from_panel(db, 'all')
        rows = list((await db.scalars(select(Subscription))).all())
        assert stats['errors'] == 0
        assert len(rows) == 1
        assert rows[0].is_trial is trial
        assert rows[0].end_date == END
        assert rows[0].device_limit == 5
        assert (await db.get(User, 1)).balance_kopeks == 0
        assert list((await db.scalars(select(Transaction))).all()) == []


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('second_daily', [False, True])
async def test_ambiguous_squads_do_not_assign_a_tariff(postgres_database, monkeypatch, reverse, second_daily):
    svc = service(monkeypatch, [account()])
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db)
        tariffs = [tariff(1), tariff(2, daily=second_daily)]
        db.add_all(tariffs[::-1] if reverse else tariffs)
        await db.commit()
        stats = await svc.sync_users_from_panel(db, 'all')
        assert stats['errors'] == 0
        sub = (await db.scalars(select(Subscription))).one()
        assert sub.tariff_id is None
        assert sub.is_daily_tariff is False
        assert list((await db.scalars(select(Transaction))).all()) == []
        assert (await db.get(User, 1)).balance_kopeks == 0


@pytest.mark.parametrize('daily', [False, True])
async def test_unique_tariff_mapping_is_preserved(postgres_database, monkeypatch, daily):
    svc = service(monkeypatch, [account()])
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db)
        db.add(tariff(daily=daily))
        await db.commit()
        stats = await svc.sync_users_from_panel(db, 'all')
        assert stats['errors'] == 0
        assert (await db.scalars(select(Subscription.tariff_id))).one() == 1


@pytest.mark.parametrize('old_panel_id', [None, 8999])
@pytest.mark.parametrize('owner_panel_id', [None, 9001])
@pytest.mark.parametrize('identity', ['short', 'username'])
@pytest.mark.parametrize('trial', [False, True])
async def test_exact_relink_preserves_billing_row_and_addons(
    postgres_database, monkeypatch, old_panel_id, owner_panel_id, identity, trial
):
    svc = service(monkeypatch, [account(tag='TRIAL' if not trial else 'PAID')])
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db, user_panel_id=owner_panel_id)
        db.add(tariff())
        await db.commit()
        sub = Subscription(
            id=10,
            user_id=1,
            tariff_id=1,
            remnawave_id=old_panel_id,
            remnawave_short_uuid='shared-short' if identity == 'short' else None,
            remnawave_short_id='local10' if identity == 'username' else 'other10',
            status='active',
            is_trial=trial,
            start_date=NOW - timedelta(days=10),
            end_date=END,
            traffic_limit_gb=120,
            purchased_traffic_gb=20,
            device_limit=5,
            connected_squads=['test-squad'],
        )
        db.add(sub)
        await db.commit()
        db.add(TrafficPurchase(id=1, subscription_id=10, traffic_gb=20, expires_at=END))
        await db.commit()
        for _ in range(2):
            stats = await svc.sync_users_from_panel(db, 'all')
            assert stats['errors'] == 0
            assert stats['created'] == 0
        rows = list((await db.scalars(select(Subscription))).all())
        assert len(rows) == 1
        await db.refresh(sub)
        assert sub.remnawave_id == 9001
        assert sub.is_trial is trial
        assert (sub.tariff_id, sub.end_date, sub.device_limit, sub.traffic_limit_gb, sub.purchased_traffic_gb) == (
            1,
            END,
            5,
            120,
            20,
        )
        assert await db.get(TrafficPurchase, 1) is not None
        assert list((await db.scalars(select(Transaction))).all()) == []


async def test_exact_short_identity_precedes_legacy_longest_subscription(postgres_database, monkeypatch):
    svc = service(monkeypatch, [account()])
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db, user_panel_id=9001)
        target = Subscription(
            id=10,
            user_id=1,
            remnawave_short_id='target10',
            remnawave_short_uuid='shared-short',
            status='active',
            is_trial=True,
            end_date=END,
        )
        decoy = Subscription(
            id=11,
            user_id=1,
            remnawave_short_id='decoy11',
            remnawave_short_uuid='other-short',
            status='active',
            is_trial=False,
            end_date=END + timedelta(days=100),
        )
        db.add_all([target, decoy])
        await db.commit()
        stats = await svc.sync_users_from_panel(db, 'all')
        assert stats['errors'] == 0
        await db.refresh(target)
        await db.refresh(decoy)
        assert target.remnawave_id == 9001
        assert decoy.remnawave_id is None
        assert decoy.end_date == END + timedelta(days=100)
        assert target.is_trial is True


async def test_concurrent_imports_respect_subscription_cap(postgres_database, monkeypatch):
    from app.database.crud import subscription as subscriptions

    first_svc = service(monkeypatch, [account()])
    second_svc = service(monkeypatch, [account(panel_id=9002, short='second-short', username='account_local20')])
    monkeypatch.setattr(settings, 'MAX_ACTIVE_SUBSCRIPTIONS', 1)
    async with postgres_sessions(postgres_database, TABLES, count=3) as (left, right, observer):
        await seed(left)
        entered = asyncio.Event()
        second_entered = asyncio.Event()
        release = asyncio.Event()
        generate = subscriptions.generate_unique_short_id

        async def pause_first_import(db, *args, **kwargs):
            value = await generate(db, *args, **kwargs)
            if db is left:
                entered.set()
                await asyncio.wait_for(release.wait(), 10)
            else:
                second_entered.set()
            return value

        monkeypatch.setattr(subscriptions, 'generate_unique_short_id', pause_first_import)
        first = asyncio.create_task(first_svc.sync_users_from_panel(left, 'all'))
        await asyncio.wait_for(entered.wait(), 10)
        second = asyncio.create_task(second_svc.sync_users_from_panel(right, 'all'))
        waiting = asyncio.create_task(wait_for_lock_waiter(observer))
        duplicating = asyncio.create_task(second_entered.wait())
        try:
            done, _ = await asyncio.wait({waiting, duplicating}, timeout=7, return_when=asyncio.FIRST_COMPLETED)
            assert done
            for task in done:
                await task
        finally:
            release.set()
            for task in (waiting, duplicating):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, duplicating, return_exceptions=True)
        results = await asyncio.wait_for(asyncio.gather(first, second), 10)
        await observer.rollback()
        rows = list((await observer.scalars(select(Subscription))).all())
        assert len(rows) == 1
        assert sum(result['created'] for result in results) == 1
        assert sum(result['errors'] for result in results) == 0


async def test_panel_snapshot_does_not_recreate_deleted_account(postgres_database, monkeypatch):
    from app.cabinet.routes.subscription_modules.multi_tariff import delete_subscription
    from app.services.subscription_service import SubscriptionService

    svc = service(monkeypatch, [account()])
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'delete')
    loaded = asyncio.Event()
    release = asyncio.Event()

    async def snapshot(**kwargs):
        loaded.set()
        await asyncio.wait_for(release.wait(), 10)
        return {'users': [account()], 'hasMore': False, 'nextCursor': None}

    svc.test_api.get_all_users_page_stream.side_effect = snapshot
    deleted_panel = AsyncMock(return_value=True)
    monkeypatch.setattr(SubscriptionService, 'delete_remnawave_user', deleted_panel)
    async with postgres_sessions(postgres_database, TABLES) as (sync_db, delete_db):
        await seed(delete_db, user_panel_id=9001)
        delete_db.add(
            Subscription(
                id=10,
                user_id=1,
                status='expired',
                is_trial=False,
                end_date=NOW - timedelta(days=1),
                remnawave_id=9001,
                remnawave_short_uuid='shared-short',
                remnawave_short_id='local10',
            )
        )
        await delete_db.commit()
        syncing = asyncio.create_task(svc.sync_users_from_panel(sync_db, 'all'))
        await asyncio.wait_for(loaded.wait(), 10)
        await delete_subscription(10, await delete_db.get(User, 1), delete_db)
        svc.test_api.get_user_by_id.side_effect = None
        svc.test_api.get_user_by_id.return_value = None
        release.set()
        result = await asyncio.wait_for(syncing, 10)
        assert result['created'] == 0
        assert result['errors'] == 0
        assert list((await sync_db.scalars(select(Subscription))).all()) == []
        deleted_panel.assert_awaited_once_with(9001)


async def test_refreshing_owner_does_not_discard_previous_account_update(postgres_database, monkeypatch):
    svc = service(monkeypatch, [account(), account(panel_id=9002, short='second-short', username='account_local20')])
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db)
        for sub_id, panel_id in [(10, 9001), (11, 9002)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=1,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'local{sub_id}',
                    status='active',
                    is_trial=False,
                    end_date=END,
                    device_limit=1,
                    traffic_limit_gb=100,
                )
            )
        await db.commit()
        result = await svc.sync_users_from_panel(db, 'all')
        assert result['errors'] == 0
        db.expire_all()
        rows = list((await db.scalars(select(Subscription).order_by(Subscription.id))).all())
        assert [(row.device_limit, row.traffic_limit_gb) for row in rows] == [(5, 120), (5, 120)]


async def test_ambiguous_legacy_identity_does_not_pick_an_arbitrary_row(postgres_database, monkeypatch):
    svc = service(monkeypatch, [account(short='unknown', username='unknown')])
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db, user_panel_id=9001)
        for sub_id in [10, 11]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=1,
                    remnawave_short_id=f'local{sub_id}',
                    status='active',
                    is_trial=False,
                    end_date=END + timedelta(days=sub_id),
                )
            )
        await db.commit()
        result = await svc.sync_users_from_panel(db, 'all')
        rows = list((await db.scalars(select(Subscription))).all())
        assert result['created'] == result['updated'] == 0
        assert result['errors'] == 1
        assert len(rows) == 2
        assert all(row.remnawave_id is None for row in rows)


async def test_current_panel_owner_change_does_not_import_foreign_account(postgres_database, monkeypatch):
    svc = service(monkeypatch, [account()])
    foreign = account()
    foreign.telegram_id = 202
    svc.test_api.get_user_by_id.side_effect = None
    svc.test_api.get_user_by_id.return_value = foreign
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db)
        result = await svc.sync_users_from_panel(db, 'all')
        assert result['created'] == 0
        assert result['errors'] == 1
        assert list((await db.scalars(select(Subscription))).all()) == []


async def test_parallel_reversed_pages_do_not_deadlock_owner_locks(postgres_database, monkeypatch):
    from app.database.crud import subscription as subscriptions

    second_account = account(panel_id=9002, short='second-short', username='account_local20')
    second_account.telegram_id = 102
    first_svc = service(monkeypatch, [account(), second_account])
    second_svc = service(monkeypatch, [second_account, account()])
    async with postgres_sessions(postgres_database, TABLES, count=3) as (left, right, observer):
        await seed(left)
        left.add(User(id=2, telegram_id=102, status='active'))
        await left.commit()
        entered = asyncio.Event()
        release = asyncio.Event()
        generate = subscriptions.generate_unique_short_id

        async def pause_first_account(db, *args, **kwargs):
            value = await generate(db, *args, **kwargs)
            if db is left and not entered.is_set():
                entered.set()
                await asyncio.wait_for(release.wait(), 10)
            return value

        monkeypatch.setattr(subscriptions, 'generate_unique_short_id', pause_first_account)
        first = asyncio.create_task(first_svc.sync_users_from_panel(left, 'all'))
        await asyncio.wait_for(entered.wait(), 10)
        second = asyncio.create_task(second_svc.sync_users_from_panel(right, 'all'))
        try:
            await wait_for_lock_waiter(observer)
        finally:
            release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert sum(result['created'] for result in results) == 2
        assert sum(result['errors'] for result in results) == 0
        await observer.rollback()
        assert len(list((await observer.scalars(select(Subscription))).all())) == 2


async def test_trial_marker_reads_live_database_override(postgres_database, monkeypatch):
    from app.services.settings_store import store_setting
    from app.services.system_settings_service import bot_configuration_service as configuration

    accounts = [account(tag='TRY')]
    svc = service(monkeypatch, accounts)
    configuration.initialize_definitions()
    monkeypatch.setattr(configuration, '_env_override_keys', set())
    monkeypatch.setattr(configuration, '_overrides_raw', {})
    async with postgres_session(postgres_database, TABLES) as db:
        await seed(db)
        assert await store_setting(db, 'TRIAL_USER_TAG', 'TRY') is True
        assert (await svc.sync_users_from_panel(db, 'all'))['errors'] == 0
        first = (await db.scalars(select(Subscription))).one()
        assert first.is_trial is True
        assert await store_setting(db, 'TRIAL_USER_TAG', 'TRIAL') is True
        accounts[0] = account(tag='TRY', panel_id=9002, short='second-short', username='account_local20')
        assert (await svc.sync_users_from_panel(db, 'all'))['errors'] == 0
        rows = list((await db.scalars(select(Subscription).order_by(Subscription.remnawave_id))).all())
        assert [row.is_trial for row in rows] == [True, False]


@pytest.mark.parametrize('ambiguous', [False, True])
async def test_cabinet_reports_partial_import_errors(postgres_database, monkeypatch, ambiguous):
    from app.cabinet.routes import admin_remnawave as route

    svc = service(monkeypatch, [account(short='unknown', username='unknown')])
    monkeypatch.setattr(route, '_get_service', lambda: svc)
    monkeypatch.setattr(route, '_ensure_configured', lambda _: None)
    async with postgres_session(postgres_database, TABLES) as db:
        user = await seed(db, user_panel_id=9001)
        if ambiguous:
            for sub_id in [10, 11]:
                db.add(
                    Subscription(
                        id=sub_id,
                        user_id=1,
                        remnawave_short_id=f'local{sub_id}',
                        status='active',
                        is_trial=False,
                        end_date=END,
                    )
                )
            await db.commit()
        response = await route.sync_from_panel(route.SyncMode(mode='all'), admin=user, db=db)
        assert response.data['errors'] == int(ambiguous)
        assert response.success is (not ambiguous)
