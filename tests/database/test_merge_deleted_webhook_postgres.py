"""A merged-away panel account must never resolve to the surviving subscription."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import account_linking
from app.config import settings
from app.database.models import Base, Subscription, Transaction, User
from app.services import account_merge_service as merge
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from tests.fixtures.postgres_db import postgres_session, postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.fixture
def panel(monkeypatch):
    actions = []

    async def delete(ident):
        actions.append(('delete', ident))

    async def disable(ident):
        actions.append(('disable', ident))

    @asynccontextmanager
    async def api():
        yield SimpleNamespace(delete_user=delete, disable_user=disable)

    monkeypatch.setattr(merge, '_get_remnawave_api', api)
    return actions


@pytest.mark.parametrize('secondary_balance', [0, 800])
@pytest.mark.parametrize('keep', [1, 2])
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('trial', [False, True])
@pytest.mark.parametrize('mode', ['delete', 'disable'])
async def test_late_deleted_event_after_email_telegram_merge_preserves_paid_subscription(
    postgres_database, monkeypatch, panel, secondary_balance, keep, legacy, trial, mode
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', mode)
    monkeypatch.setattr(merge, '_sync_transferred_subscriptions_to_panel', AsyncMock())
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_telegram_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_recent_recreations', {})
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        primary = User(
            id=1,
            email='primary@example.com',
            email_verified=True,
            status='active',
            language='ru',
            balance_kopeks=1200,
            remnawave_id=9001,
            telegram_id=102 if keep == 2 else None,
        )
        secondary = User(
            id=2,
            telegram_id=102 if keep == 1 else None,
            status='active',
            language='ru',
            balance_kopeks=secondary_balance,
            remnawave_id=9002,
        )
        db.add_all([primary, secondary])
        for ident, days in [(1, 365), (2, 30)]:
            db.add(
                Subscription(
                    id=ident,
                    user_id=ident,
                    status='trial' if trial else 'active',
                    is_trial=trial,
                    start_date=now,
                    end_date=now + timedelta(days=days),
                    remnawave_id=None if legacy else 9000 + ident,
                    remnawave_short_uuid=f'masked-{ident}',
                    remnawave_short_id=f'local-{ident}',
                    subscription_url=f'https://subscription.example/masked-{ident}',
                    connected_squads=['masked-squad'],
                    traffic_limit_gb=100,
                    device_limit=5,
                )
            )
        await db.commit()
        kept = await db.get(Subscription, keep)
        before = (
            kept.status,
            kept.end_date,
            kept.remnawave_id,
            kept.remnawave_short_uuid,
            kept.subscription_url,
            kept.connected_squads,
            kept.device_limit,
            kept.traffic_limit_gb,
        )
        monkeypatch.setattr(account_linking.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
        monkeypatch.setattr(
            account_linking,
            'consume_merge_token',
            AsyncMock(side_effect=[{'primary_user_id': 1, 'secondary_user_id': 2}, None]),
        )
        app = FastAPI()
        app.include_router(account_linking.merge_router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: db
        app.dependency_overrides[get_current_cabinet_user] = lambda: primary
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/cabinet/auth/merge/' + 'a' * 32, json={'keep_subscription_from': keep})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['success'] and result['access_token'] and result['refresh_token']
            assert result['user']['telegram_id'] == 102
            assert result['user']['balance_kopeks'] == 1200 + secondary_balance
            replay = await client.post('/cabinet/auth/merge/' + 'a' * 32, json={'keep_subscription_from': keep})
            assert replay.status_code == 404
        removed = 3 - keep
        assert panel == [(mode, 9000 + removed)]
        assert primary.telegram_id == 102
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        for _ in range(2):
            # A delayed event must be safe without any process-local marker.
            service._intentional_panel_deletions_by_id.clear()
            service._intentional_panel_deletions_by_telegram_id.clear()
            service._recent_recreations.clear()
            await service._process_user_event(
                db,
                'user.deleted',
                {'id': 9000 + removed, 'shortUuid': f'masked-{removed}', 'telegramId': 102},
                service._handle_user_deleted,
            )
            await db.refresh(kept)
            await db.refresh(primary)
            assert (
                kept.status,
                kept.end_date,
                kept.remnawave_id,
                kept.remnawave_short_uuid,
                kept.subscription_url,
                kept.connected_squads,
                kept.device_limit,
                kept.traffic_limit_gb,
            ) == before
            assert primary.remnawave_id == 9000 + keep
            assert primary.balance_kopeks == 1200 + secondary_balance
            assert list(await db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('identity', ['numeric', 'short', 'legacy', 'unknown', 'conflicting', 'legacy-conflicting'])
async def test_deleted_event_targets_only_current_identity(postgres_database, monkeypatch, multi, identity):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_telegram_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_recent_recreations', {})
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=9001, balance_kopeks=1200)
        sub = Subscription(
            id=1,
            user_id=1,
            status='active',
            is_trial=False,
            start_date=now,
            end_date=now + timedelta(days=365),
            remnawave_id=None if identity.startswith('legacy') else 9001,
            remnawave_short_uuid=None if identity == 'legacy' else 'masked-current',
            subscription_url='https://subscription.example/masked-current',
            connected_squads=['masked-squad'],
        )
        db.add_all([user, sub])
        await db.commit()
        payload = {'telegramId': 101}
        if identity == 'short':
            payload['shortUuid'] = 'masked-current'
        else:
            payload['id'] = 9002 if identity in ('unknown', 'conflicting', 'legacy-conflicting') else 9001
            if identity in ('conflicting', 'legacy-conflicting'):
                payload['shortUuid'] = 'masked-current'
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        await service._process_user_event(db, 'user.deleted', payload, service._handle_user_deleted)
        await db.refresh(sub)
        await db.refresh(user)
        # Multi mode cannot address an individual subscription by User alone.
        deleted = identity in ('numeric', 'short') or (identity == 'legacy' and not multi)
        # In multi mode shortUuid identifies its own account independently of User.
        deleted = deleted or (identity == 'legacy-conflicting' and multi)
        assert sub.status == ('expired' if deleted else 'active')
        assert sub.subscription_url == (None if deleted else 'https://subscription.example/masked-current')
        if identity in ('unknown', 'conflicting') or (identity == 'legacy-conflicting' and not multi):
            assert user.remnawave_id == 9001
        assert user.balance_kopeks == 1200


async def test_rotated_short_id_keeps_legacy_numeric_identity(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=9001))
        sub = Subscription(
            id=1,
            user_id=1,
            status='active',
            start_date=now,
            end_date=now + timedelta(days=30),
            remnawave_short_uuid='masked-old',
            subscription_url='https://subscription.example/old',
        )
        db.add(sub)
        await db.commit()
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        await service._process_user_event(
            db,
            'user.revoked',
            {
                'id': 9001,
                'telegramId': 101,
                'shortUuid': 'masked-new',
                'subscriptionUrl': 'https://subscription.example/new',
            },
            service._handle_user_revoked,
        )
        await db.refresh(sub)
        assert sub.subscription_url == 'https://subscription.example/new'
        assert sub.status == 'active'


async def test_deleted_event_resolved_before_merge_commit_cannot_clear_survivor_identity(
    postgres_database, monkeypatch
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_telegram_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_recent_recreations', {})
    now = datetime.now(UTC)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, webhook_db):
        db.add_all(
            [
                User(id=1, telegram_id=101, status='active', language='ru', remnawave_id=9001),
                User(id=2, email='secondary@example.com', status='active', language='ru', remnawave_id=9002),
            ]
        )
        for ident in (1, 2):
            db.add(
                Subscription(
                    id=ident,
                    user_id=ident,
                    status='active',
                    start_date=now,
                    end_date=now + timedelta(days=30),
                    remnawave_id=9000 + ident,
                    remnawave_short_uuid=f'masked-{ident}',
                    remnawave_short_id=f'local-{ident}',
                    subscription_url=f'https://subscription.example/{ident}',
                )
            )
        await db.commit()
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        resolved, resume = asyncio.Event(), asyncio.Event()
        original = service._resolve_user_and_subscription

        async def pause_after_first_resolve(*args):
            result = await original(*args)
            if not resolved.is_set():
                resolved.set()
                await resume.wait()
            return result

        monkeypatch.setattr(service, '_resolve_user_and_subscription', pause_after_first_resolve)
        task = asyncio.create_task(
            service._process_user_event(
                webhook_db,
                'user.deleted',
                {'id': 9001, 'telegramId': 101, 'shortUuid': 'masked-1'},
                service._handle_user_deleted,
            )
        )
        try:
            await asyncio.wait_for(resolved.wait(), 5)
            await merge.execute_merge(db, 1, 2, keep_subscription_from='secondary', deferred_remnawave_deletions=[])
            await db.commit()
            resume.set()
            await asyncio.wait_for(task, 10)
            user = await db.get(User, 1, populate_existing=True)
            kept = await db.get(Subscription, 2, populate_existing=True)
            assert user.remnawave_id == 9002
            assert kept.remnawave_id == 9002
            assert kept.status == 'active'
            assert kept.subscription_url == 'https://subscription.example/2'
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_multi_merge_keeps_both_identities_independent(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(merge, '_sync_transferred_subscriptions_to_panel', AsyncMock())
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_intentional_panel_deletions_by_telegram_id', {})
    monkeypatch.setattr(RemnaWaveWebhookService, '_recent_recreations', {})
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, email='primary@example.com', status='active', language='ru', remnawave_id=9001),
                User(id=2, telegram_id=102, status='active', language='ru', remnawave_id=9002, balance_kopeks=800),
            ]
        )
        for ident in (1, 2):
            db.add(
                Subscription(
                    id=ident,
                    user_id=ident,
                    status='active',
                    is_trial=False,
                    start_date=now,
                    end_date=now + timedelta(days=ident * 30),
                    remnawave_id=9000 + ident,
                    remnawave_short_uuid=f'masked-{ident}',
                    remnawave_short_id=f'local-{ident}',
                    subscription_url=f'https://subscription.example/{ident}',
                    connected_squads=['masked-squad'],
                )
            )
        await db.commit()
        pending = []
        user = await merge.execute_merge(db, 1, 2, deferred_remnawave_deletions=pending)
        await db.commit()
        assert pending == []
        rows = list(await db.scalars(select(Subscription).order_by(Subscription.id)))
        assert [row.user_id for row in rows] == [1, 1]
        assert [row.remnawave_id for row in rows] == [9001, 9002]
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        for panel_id in (9003, 9002, 9002):
            service._recent_recreations.clear()
            await service._process_user_event(
                db, 'user.deleted', {'id': panel_id, 'telegramId': 102}, service._handle_user_deleted
            )
            for row in rows:
                await db.refresh(row)
            assert rows[0].status == 'active'
            assert rows[0].remnawave_id == 9001
            assert rows[0].subscription_url == 'https://subscription.example/1'
            assert rows[0].end_date == now + timedelta(days=30)
            assert rows[1].status == ('active' if panel_id == 9003 else 'expired')
            await db.refresh(user)
            assert user.balance_kopeks == 800
            assert user.telegram_id == 102
