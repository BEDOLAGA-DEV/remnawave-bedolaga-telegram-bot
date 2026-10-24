"""TG-101: panel rollback must not corrupt promo effects or consume a rejected trial."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes import promocode as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, PromoCode, PromoCodeUse, Subscription, Tariff, User
from app.services.promocode_service import PromoCodeService
from app.services.remnawave_retry_queue import RemnaWaveRetryQueue
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('failure', ['none', 'rollback', 'paid_during_sync', 'bound_trial'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('entry', ['service', 'cabinet'])
async def test_trial_provisioning_failure_releases_usage_and_allows_retry(
    postgres_database, monkeypatch, failure, multi, entry
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

    async def failed_panel(self, db, subscription, **kwargs):
        if failure in ('paid_during_sync', 'bound_trial'):
            # A completed purchase may convert the committed trial while the
            # initial panel request is in flight. Cleanup must preserve it.
            if failure == 'paid_during_sync':
                subscription.is_trial = False
                subscription.end_date += timedelta(days=30)
            subscription.remnawave_id = 9001
            await db.commit()
        if failure in ('rollback', 'paid_during_sync', 'bound_trial'):
            await db.rollback()

    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', failed_panel)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=0),
                Tariff(
                    id=1,
                    name='Trial',
                    is_active=True,
                    traffic_limit_gb=10,
                    device_limit=1,
                    period_prices={'30': 10000},
                    allowed_squads=[],
                ),
            ]
        )
        await db.commit()
        promo = PromoCode(
            id=1, code='TRIAL3', type='trial_subscription', tariff_id=1, subscription_days=3, max_uses=1, is_active=True
        )
        db.add(promo)
        await db.commit()
        user = await get_user_by_id(db, 1)
        if entry == 'service':
            result = await PromoCodeService().activate_promocode(db, 1, 'TRIAL3')
        else:
            try:
                response = await cabinet.activate_promocode(cabinet.PromocodeActivateRequest(code='TRIAL3'), user, db)
            except HTTPException as error:
                assert error.status_code == 400
                result = {'success': False, 'error': error.detail['code']}
            else:
                result = {'success': response.success}
        await db.refresh(promo)
        snapshot = {
            'error': result.get('error'),
            'subscriptions': len((await db.scalars(select(Subscription))).all()),
            'uses': len((await db.scalars(select(PromoCodeUse))).all()),
            'counter': promo.current_uses,
        }
        if failure == 'bound_trial':
            assert result['success'], result
            assert snapshot == {'error': None, 'subscriptions': 1, 'uses': 1, 'counter': 1}
            assert not (await PromoCodeService().activate_promocode(db, 1, 'TRIAL3'))['success']
            return
        assert snapshot == {
            'error': 'trial_provisioning_failed',
            'subscriptions': 1 if failure == 'paid_during_sync' else 0,
            'uses': 0,
            'counter': 0,
        }
        assert user.language == 'ru'
        if failure == 'paid_during_sync':
            assert not (await db.scalar(select(Subscription))).is_trial
            return

        async def successful_panel(self, db, subscription, **kwargs):
            subscription.remnawave_id = 9001
            await db.commit()
            return SimpleNamespace(id=9001)

        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', successful_panel)
        result = await PromoCodeService().activate_promocode(db, 1, 'TRIAL3')
        assert result['success']
        assert result['promocode']['current_uses'] == 1
        await db.refresh(promo)
        assert promo.current_uses == 1
        assert len((await db.scalars(select(PromoCodeUse))).all()) == 1
        subscriptions = (await db.scalars(select(Subscription))).all()
        assert len(subscriptions) == 1 and subscriptions[0].is_trial


@pytest.mark.parametrize('kind', ['subscription_days', 'balance_and_days', 'trial_subscription', 'traffic_only'])
@pytest.mark.parametrize('multi', [False, True])
async def test_existing_subscription_promo_survives_panel_rollback(postgres_database, monkeypatch, kind, multi):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    queue = RemnaWaveRetryQueue()
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue', queue)

    async def failed_panel(self, db, subscription, **kwargs):
        await db.rollback()

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', failed_panel)
    now = datetime.now(UTC)
    end = now + timedelta(days=5)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=0),
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=1,
                    period_prices={'30': 10000},
                    allowed_squads=[],
                ),
            ]
        )
        await db.commit()
        sub = Subscription(
            id=1,
            user_id=1,
            tariff_id=1,
            status='active',
            is_trial=kind == 'trial_subscription',
            device_limit=1,
            traffic_limit_gb=100,
            traffic_used_gb=0,
            connected_squads=[],
            start_date=now - timedelta(days=1),
            end_date=end,
            remnawave_id=9001,
        )
        promo = PromoCode(
            id=1,
            code='GRANT3',
            type='balance_and_days' if kind == 'traffic_only' else kind,
            tariff_id=1,
            subscription_days=0 if kind == 'traffic_only' else 3,
            balance_bonus_kopeks=5000 if kind == 'balance_and_days' else 0,
            traffic_gb=20 if kind in ('balance_and_days', 'traffic_only') else 0,
            max_uses=1,
            is_active=True,
        )
        db.add_all([sub, promo])
        await db.commit()
        user = await get_user_by_id(db, 1)
        result = await PromoCodeService().activate_promocode(db, 1, 'GRANT3', subscription_id=1)
        assert result['success'], result
        assert result['promocode']['current_uses'] == 1
        await db.refresh(sub)
        await db.refresh(promo)
        await db.refresh(user)
        assert sub.end_date == end + timedelta(days=0 if kind == 'traffic_only' else 3)
        assert sub.pending_panel_expire_at == (None if kind == 'traffic_only' else sub.end_date)
        assert sub.is_trial is (kind == 'trial_subscription')
        assert promo.current_uses == 1
        assert user.balance_kopeks == (5000 if kind == 'balance_and_days' else 0)
        assert sub.traffic_limit_gb == (120 if kind in ('balance_and_days', 'traffic_only') else 100)
        assert queue.pending_count == 1
        assert queue._queue[0].subscription_id == sub.id and not queue._queue[0].reset_traffic
        assert len((await db.scalars(select(PromoCodeUse))).all()) == 1
        repeated = await PromoCodeService().activate_promocode(db, 1, 'GRANT3', subscription_id=1)
        assert not repeated['success']


async def test_failed_trial_cleanup_waits_for_concurrent_paid_conversion(postgres_database, monkeypatch):
    from app.database.crud.user import lock_user_for_pricing

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    ready, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def failed_panel(self, db, subscription, **kwargs):
        sub_id = subscription.id

        async def convert():
            async with AsyncSession(db.bind, expire_on_commit=False) as editor:
                await lock_user_for_pricing(editor, 1)
                sub = await editor.scalar(select(Subscription).where(Subscription.id == sub_id).with_for_update())
                sub.is_trial = False
                sub.remnawave_id = 9001
                sub.end_date += timedelta(days=30)
                await editor.flush()
                ready.set()
                await asyncio.wait_for(release.wait(), 10)
                await editor.commit()

        tasks.append(asyncio.create_task(convert()))
        await asyncio.wait_for(ready.wait(), 10)
        await db.rollback()

    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', failed_panel)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, language='ru', status='active'))
        db.add(
            PromoCode(id=1, code='RACE3', type='trial_subscription', subscription_days=3, max_uses=1, is_active=True)
        )
        await db.commit()
        task = asyncio.create_task(PromoCodeService().activate_promocode(db, 1, 'RACE3'))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            async with AsyncSession(db.bind) as observer:
                waiting = await lock_waiter_appeared(observer)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            result = await asyncio.wait_for(task, 10)
            assert waiting, 'Cleanup must wait for the real concurrent purchase lock'
        finally:
            release.set()
            for pending in [task, *tasks]:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, *tasks, return_exceptions=True)
        assert result == {'success': False, 'error': 'trial_provisioning_failed'}
        sub = await db.scalar(select(Subscription))
        assert sub is not None and not sub.is_trial and sub.remnawave_id == 9001
        assert not (await db.scalars(select(PromoCodeUse))).all()
        assert (await db.get(PromoCode, 1)).current_uses == 0


async def test_trial_compensation_preserves_another_users_committed_activation(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

    async def panel(self, db, subscription, **kwargs):
        if subscription.user_id == 2:
            subscription.remnawave_id = 9002
            await db.commit()
            return SimpleNamespace(id=9002)
        # First activation has committed its reservation. Another user claims
        # the second slot while its panel request is still outstanding.
        async with AsyncSession(db.bind, expire_on_commit=False) as other:
            result = await PromoCodeService().activate_promocode(other, 2, 'SHARED3')
            assert result['success']
        await db.rollback()
        return None

    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', panel)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru', status='active'),
                User(id=2, telegram_id=102, language='ru', status='active'),
                PromoCode(
                    id=1, code='SHARED3', type='trial_subscription', subscription_days=3, max_uses=2, is_active=True
                ),
            ]
        )
        await db.commit()
        result = await PromoCodeService().activate_promocode(db, 1, 'SHARED3')
        assert result == {'success': False, 'error': 'trial_provisioning_failed'}
        assert (await db.get(PromoCode, 1)).current_uses == 1
        assert list(await db.scalars(select(PromoCodeUse.user_id))) == [2]
        assert list(await db.scalars(select(Subscription.user_id))) == [2]
