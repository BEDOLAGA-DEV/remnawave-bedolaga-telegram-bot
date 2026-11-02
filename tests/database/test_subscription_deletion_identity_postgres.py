"""Deleting a duplicate must preserve the panel account its neighbour uses."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes.subscription_modules.multi_tariff import delete_subscription
from app.config import settings
from app.database.models import Base, Subscription, User
from app.services import subscription_deletion_service as deletion
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session, postgres_sessions, wait_for_lock_waiter


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)
PANEL_ID = 9001


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'delete')
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_telegram_id', {})
    api = SimpleNamespace(
        get_user_by_id=AsyncMock(return_value=SimpleNamespace(id=PANEL_ID, short_uuid='shared-short'))
    )
    calls = SimpleNamespace(deleted=[], disabled=[], api=api)

    @asynccontextmanager
    async def client(self):
        yield api

    async def remove(self, panel_user_id):
        calls.deleted.append(panel_user_id)
        return True

    async def disable(self, panel_user_id, db=None):
        calls.disabled.append(panel_user_id)
        return True

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    monkeypatch.setattr(SubscriptionService, 'delete_remnawave_user', remove)
    monkeypatch.setattr(SubscriptionService, 'disable_remnawave_user', disable)
    return calls


def subscription(sub_id, *, owner=1, status='expired', panel_id=None, short=None):
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=owner,
        status=status,
        is_trial=status == 'trial',
        start_date=now - timedelta(days=30),
        end_date=now + timedelta(days=-1 if status == 'expired' else 5),
        remnawave_id=panel_id,
        remnawave_short_uuid=short,
        remnawave_short_id=f'local-{sub_id}',
        subscription_url=f'https://subscription.example/{short or sub_id}',
        connected_squads=[],
    )


async def seed(db, *, target_short='shared-short'):
    user = User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=PANEL_ID)
    target = subscription(10, panel_id=PANEL_ID, short=target_short)
    db.add_all([user, target])
    await db.commit()
    return user, target


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('mode', ['delete', 'disable'])
@pytest.mark.parametrize('alias', ['local', 'remote', 'distinct', 'remote-distinct'])
@pytest.mark.parametrize('sibling_status', ['active', 'trial', 'limited', 'expired'])
async def test_deletion_spares_referenced_identity(
    postgres_database, monkeypatch, panel, multi, mode, alias, sibling_status
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', mode)
    async with postgres_session(postgres_database, TABLES) as db:
        user, target = await seed(db, target_short=None if alias.startswith('remote') else 'shared-short')
        sibling = subscription(
            11, status=sibling_status, short='other-short' if 'distinct' in alias else 'shared-short'
        )
        db.add(sibling)
        await db.commit()
        before = (sibling.status, sibling.end_date, sibling.subscription_url)
        await delete_subscription(target.id, user, db)
        assert await db.scalar(select(Subscription.id).where(Subscription.id == 10)) is None
        await db.refresh(sibling)
        assert (sibling.status, sibling.end_date, sibling.subscription_url) == before
        protect = (multi and 'distinct' not in alias) or (not multi and sibling_status != 'expired')
        assert panel.deleted == ([] if protect or not multi or mode == 'disable' else [PANEL_ID])
        assert panel.disabled == ([] if protect or (multi and mode == 'delete') else [PANEL_ID])
        await db.refresh(user)
        assert user.remnawave_id == (None if panel.deleted else PANEL_ID)
        assert RemnaWaveWebhookService._is_intentional_panel_deletion_event({'id': PANEL_ID}) is bool(panel.deleted)


@pytest.mark.parametrize('missing_user_id', [False, True])
async def test_shared_short_identity_survives_legacy_or_foreign_reference(
    postgres_database, monkeypatch, panel, missing_user_id
):
    async with postgres_session(postgres_database, TABLES) as db:
        user, target = await seed(db)
        if missing_user_id:
            monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
            user.remnawave_id = None
            owner = 1
        else:
            db.add(User(id=2, telegram_id=102, status='active'))
            owner = 2
        sibling = subscription(11, owner=owner, status='active', short='shared-short')
        db.add(sibling)
        await db.commit()
        await delete_subscription(target.id, user, db)
        assert panel.deleted == []
        assert panel.disabled == []
        assert await db.get(Subscription, 11) is not None


async def test_unavailable_identity_lookup_spares_possible_sibling(postgres_database, panel):
    panel.api.get_user_by_id.side_effect = RuntimeError('panel unavailable')
    async with postgres_session(postgres_database, TABLES) as db:
        user, target = await seed(db, target_short=None)
        db.add(subscription(11, status='active', short='shared-short'))
        await db.commit()
        await delete_subscription(target.id, user, db)
        assert panel.deleted == []
        assert panel.disabled == []
        assert await db.get(Subscription, 11) is not None


async def test_foreign_owner_numeric_identity_is_not_deleted(postgres_database, panel):
    async with postgres_session(postgres_database, TABLES) as db:
        user, target = await seed(db)
        user.remnawave_id = None
        await db.commit()
        db.add(User(id=2, telegram_id=102, remnawave_id=PANEL_ID, status='active'))
        await db.commit()
        await delete_subscription(target.id, user, db)
        assert panel.deleted == panel.disabled == []
        assert (await db.get(User, 2)).remnawave_id == PANEL_ID


@pytest.mark.parametrize('historical_id', [None, 9002])
async def test_single_tariff_checks_short_uuid_of_actual_panel_target(
    postgres_database, monkeypatch, panel, historical_id
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    async with postgres_session(postgres_database, TABLES) as db:
        user, target = await seed(db, target_short='different-account')
        target.remnawave_id = historical_id
        db.add(User(id=2, telegram_id=102, status='active'))
        db.add(subscription(11, owner=2, status='active', short='shared-short'))
        await db.commit()
        await delete_subscription(target.id, user, db)
        assert panel.deleted == panel.disabled == []
        assert await db.get(Subscription, 11) is not None
        panel.api.get_user_by_id.assert_awaited_once_with(PANEL_ID)


@pytest.mark.parametrize('state', ['pending', 'active', 'restoring'])
async def test_open_grace_rejects_delete_without_panel_changes(postgres_database, panel, state):
    from app.database.models import GraceAccessSessionModel

    now = datetime.now(UTC)
    async with postgres_session(postgres_database, TABLES) as db:
        user, target = await seed(db)
        db.add(
            GraceAccessSessionModel(
                id='00000000-0000-0000-0000-000000000001',
                subscription_id=10,
                remnawave_id=PANEL_ID,
                reason='expired',
                incident_key='test-expired',
                state=state,
                billing_before={},
                panel_before={},
                overlay={},
                started_at=now,
                grace_until=now + timedelta(hours=1),
                updated_at=now,
            )
        )
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await delete_subscription(target.id, user, db)
        assert error.value.status_code == 409
        assert await db.get(Subscription, 10) is not None
        assert panel.deleted == panel.disabled == []


@pytest.mark.parametrize(('owner', 'status', 'code'), [(2, 'expired', 404), (1, 'active', 400), (1, 'trial', 400)])
async def test_cabinet_rejects_unowned_or_live_subscription(postgres_database, panel, owner, status, code):
    async with postgres_session(postgres_database, TABLES) as db:
        user, target = await seed(db)
        if owner != 1:
            db.add(User(id=2, telegram_id=102, status='active'))
        target.user_id = owner
        target.status = status
        target.end_date = datetime.now(UTC) + timedelta(days=5)
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await delete_subscription(target.id, user, db)
        assert error.value.status_code == code
        assert await db.get(Subscription, 10) is not None
        assert panel.deleted == panel.disabled == []


async def test_concurrent_delete_reaches_panel_once(postgres_database, monkeypatch, panel):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (left, right, observer):
        _, target = await seed(left)
        stale_target = await right.get(Subscription, 10)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def paused_delete(self, panel_user_id):
            panel.deleted.append(panel_user_id)
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
            return True

        monkeypatch.setattr(SubscriptionService, 'delete_remnawave_user', paused_delete)

        async def callback(db, sub):
            try:
                await deletion.delete_subscription_record(db, sub, deleted_by='user')
            finally:
                await db.rollback()

        first = asyncio.create_task(callback(left, target))
        await asyncio.wait_for(entered.wait(), 10)
        second = asyncio.create_task(callback(right, stale_target))
        try:
            await wait_for_lock_waiter(observer)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert panel.deleted == [PANEL_ID]
        await observer.rollback()
        assert await observer.get(Subscription, 10) is None
