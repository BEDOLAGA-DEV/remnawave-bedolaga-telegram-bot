"""Committed renewal survives lost in-memory retries and stale panel events."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import Base, Subscription, Transaction, User
from app.services import remnawave_retry_queue as retry
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.services.subscription_service import SubscriptionService
from tests.database.test_expired_trial_conversion_postgres import Panel
from tests.database.test_renewal_survives_stale_panel_snapshot_postgres import (
    BALANCE,
    PRICE,
    _PanelPatchFails,
    _renew_from_cabinet,
    _seed,
)
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def isolated_renewal(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(retry, 'remnawave_retry_queue', retry.RemnaWaveRetryQueue())
    _PanelPatchFails.calls.clear()
    yield
    _PanelPatchFails.calls.clear()


@pytest.mark.parametrize('event', ['user.expired', 'user.modified'])
@pytest.mark.parametrize('age_hours', [1, 73])
async def test_paid_expiry_survives_restart_and_stale_event(postgres_database, monkeypatch, event, age_hours):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user, subscription, old_end = await _seed(db, datetime.now(UTC) - timedelta(days=3))
        user_id, subscription_id = user.id, subscription.id
        await _renew_from_cabinet(db, user, subscription, monkeypatch)
        await db.refresh(subscription)
        paid_until = subscription.end_date
        assert paid_until > datetime.now(UTC) + timedelta(days=29)
        assert retry.remnawave_retry_queue.pending_count == 1
        # The old 48-hour heuristic must not be the only persistent evidence.
        await db.execute(update(Transaction).values(created_at=datetime.now(UTC) - timedelta(hours=age_hours)))
        await db.commit()
        db.expunge_all()
        monkeypatch.setattr(retry, 'remnawave_retry_queue', retry.RemnaWaveRetryQueue())
        user = await db.get(User, user_id)
        subscription = await db.get(Subscription, subscription_id)
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        monkeypatch.setattr('app.services.grace_access_runtime.grace_access_runtime.consider_candidate', AsyncMock())
        await service._process_user_event(
            db,
            event,
            {
                'id': 555,
                'telegramId': 1,
                'status': 'EXPIRED',
                'expireAt': old_end.isoformat(),
            },
            service._handle_user_expired if event == 'user.expired' else service._handle_user_modified,
        )
        await db.refresh(subscription)
        await db.refresh(user)
        assert subscription.end_date == paid_until
        assert subscription.status == 'active'
        assert user.balance_kopeks == BALANCE - PRICE
        transactions = list(await db.scalars(select(Transaction).where(Transaction.type == 'subscription_payment')))
        assert len(transactions) == 1


async def test_restart_recovers_expiry_until_panel_confirms_without_charging_again(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'REMNAWAVE_API_URL', 'https://panel.example')
    monkeypatch.setattr(settings, 'REMNAWAVE_API_KEY', 'test')
    panel = Panel(datetime.now(UTC))
    panel.account['telegramId'] = 1

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user, subscription, _ = await _seed(db, datetime.now(UTC))
        await _renew_from_cabinet(db, user, subscription, monkeypatch)
        await db.refresh(subscription)
        target = subscription.end_date
        user.remnawave_id = subscription.remnawave_id = 9001
        await db.commit()
        monkeypatch.setattr(retry, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        panel.hwid_failures = 2
        for _ in range(2):
            # Each new worker has an empty memory queue and a one-attempt budget.
            queue = retry.RemnaWaveRetryQueue(max_retries=1)
            monkeypatch.setattr(retry, 'remnawave_retry_queue', queue)
            await queue.process_pending()
        assert len(panel.patches) == 2, 'Committed expiry must repopulate retries even after exhaustion/restart'
        queue = retry.RemnaWaveRetryQueue(max_retries=1)
        monkeypatch.setattr(retry, 'remnawave_retry_queue', queue)
        await queue.process_pending()
        await db.refresh(subscription)
        await db.refresh(user)
        assert subscription.pending_panel_expire_at is None
        assert panel._parse_user(panel.account).expire_at == target
        assert user.balance_kopeks == BALANCE - PRICE
        assert len(list(await db.scalars(select(Transaction).where(Transaction.type == 'subscription_payment')))) == 1
        await db.commit()
        await queue.process_pending()
        assert len(panel.patches) == 3, 'Acknowledged expiry must not produce endless retries'


@pytest.mark.parametrize('concurrent_change', ['renewal', 'admin-shortening', 'none'])
async def test_old_panel_ack_cannot_clear_newer_local_intent(postgres_database, concurrent_change):
    from types import SimpleNamespace

    from app.services.panel_sync.expiry_intent import acknowledge_pending_expiry

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        _, subscription, _ = await _seed(db, datetime.now(UTC))
        original = subscription.end_date
        subscription.pending_panel_expire_at = original
        await db.commit()
        sessions = async_sessionmaker(db.bind, expire_on_commit=False)
        async with sessions() as writer:
            stale = await writer.get(Subscription, subscription.id)
            if concurrent_change != 'none':
                delta = timedelta(days=30 if concurrent_change == 'renewal' else -1)
                subscription.end_date = original + delta
                subscription.pending_panel_expire_at = subscription.end_date
                await db.commit()
            await acknowledge_pending_expiry(writer, stale, SimpleNamespace(expire_at=original))
            await writer.commit()
        await db.refresh(subscription)
        if concurrent_change == 'none':
            assert subscription.pending_panel_expire_at is None
        else:
            assert subscription.pending_panel_expire_at == original + delta
            assert subscription.end_date == original + delta


@pytest.mark.parametrize('state', ['blocked', 'paused', 'grace', 'expired', 'obsolete'])
async def test_recovery_does_not_revive_ineligible_subscriptions(postgres_database, monkeypatch, state):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user, subscription, _ = await _seed(db, datetime.now(UTC))
        subscription.pending_panel_expire_at = subscription.end_date
        if state == 'blocked':
            user.status = 'blocked'
        elif state == 'paused':
            subscription.is_daily_paused = True
        elif state == 'grace':
            subscription.grace_session_open = True
        elif state == 'expired':
            subscription.end_date = subscription.pending_panel_expire_at = datetime.now(UTC) - timedelta(days=1)
        elif state == 'obsolete':
            subscription.end_date -= timedelta(days=1)
        await db.commit()
        monkeypatch.setattr(retry, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        create = AsyncMock(side_effect=AssertionError('Ineligible subscription must not be written'))
        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', create)
        await retry.RemnaWaveRetryQueue().process_pending()
        create.assert_not_called()


@pytest.mark.parametrize('confirmed', [False, True])
async def test_bonus_extension_pending_vs_confirmed_panel_change(postgres_database, monkeypatch, confirmed):
    from types import SimpleNamespace

    from app.services.panel_sync.expiry_intent import acknowledge_pending_expiry

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user, subscription, original = await _seed(db, datetime.now(UTC))
        subscription.extend_subscription(30)  # bonus days: no payment transaction
        target = subscription.end_date
        await db.commit()
        if confirmed:
            await acknowledge_pending_expiry(db, subscription, SimpleNamespace(expire_at=target))
            await db.commit()
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        await service._handle_user_modified(
            db,
            user,
            subscription,
            {
                'id': 555,
                'status': 'ACTIVE',
                'expireAt': original.isoformat(),
            },
        )
        await db.refresh(subscription)
        assert subscription.end_date == (original if confirmed else target)


@pytest.mark.parametrize('event', ['user.expired', 'panel-truth'])
async def test_pending_intent_does_not_prevent_actual_expiry(postgres_database, monkeypatch, event):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user, subscription, _ = await _seed(db, datetime.now(UTC))
        subscription.end_date = subscription.pending_panel_expire_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
        service = RemnaWaveWebhookService(AsyncMock())
        monkeypatch.setattr(service, '_notify_user', AsyncMock())
        monkeypatch.setattr('app.services.grace_access_runtime.grace_access_runtime.consider_candidate', AsyncMock())
        payload = {
            'id': 555,
            'status': 'EXPIRED',
            'expireAt': (subscription.end_date - timedelta(days=30)).isoformat(),
        }
        if event == 'panel-truth':
            from app.services.panel_sync.projection import PANEL_TRUTH, project_onto_subscription, read_panel_user

            project_onto_subscription(subscription, read_panel_user(payload), policy=PANEL_TRUTH)
            await db.commit()
        else:
            await service._process_user_event(db, event, payload, service._handle_user_expired)
        await db.refresh(subscription)
        assert subscription.status == 'expired'


@pytest.mark.parametrize('flow', ['cabinet', 'miniapp'])
async def test_trial_bonus_keeps_complete_expiry_pending(postgres_database, monkeypatch, flow):
    from types import SimpleNamespace

    from app.cabinet.routes.subscription_modules import purchase as cabinet
    from app.services import subscription_purchase_service as miniapp

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    panel = SimpleNamespace(
        create_remnawave_user=AsyncMock(side_effect=TimeoutError('test panel offline')),
        update_remnawave_user=AsyncMock(side_effect=TimeoutError('test panel offline')),
        disable_remnawave_user=AsyncMock(),
    )
    monkeypatch.setattr(cabinet, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(miniapp, 'SubscriptionService', lambda: panel)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        now = datetime.now(UTC)
        user, subscription, original = await _seed(db, now)
        trial = Subscription(
            user_id=user.id,
            status='trial',
            is_trial=True,
            start_date=now - timedelta(days=1),
            end_date=now + timedelta(days=5),
        )
        db.add(trial)
        await db.commit()
        if flow == 'cabinet':
            await cabinet.purchase_tariff(
                cabinet.TariffPurchaseRequest(
                    tariff_id=subscription.tariff_id,
                    period_days=30,
                    subscription_id=subscription.id,
                ),
                user,
                db,
            )
        else:
            pricing = SimpleNamespace(
                final_total=PRICE,
                base_original_total=PRICE,
                promo_discount_value=0,
                server_ids=[],
                months=1,
                selection=SimpleNamespace(period=SimpleNamespace(days=30), traffic_value=150, devices=3, servers=[]),
            )
            await miniapp.MiniAppSubscriptionPurchaseService().submit_purchase(
                db,
                SimpleNamespace(user=user, subscription=subscription),
                pricing,
            )
        await db.refresh(subscription)
        assert abs((subscription.end_date - original - timedelta(days=35)).total_seconds()) < 10
        assert subscription.pending_panel_expire_at == subscription.end_date
        assert len(list(await db.scalars(select(Transaction).where(Transaction.type == 'subscription_payment')))) == 1


@pytest.mark.parametrize('flow', ['pending', 'paid-trial', 'daily-charge'])
async def test_activation_and_daily_charge_persist_recoverable_expiry(postgres_database, flow):
    from app.database.crud.subscription import (
        activate_pending_subscription,
        activate_pending_trial_subscription,
        update_daily_charge_time,
    )

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user, subscription, _ = await _seed(db, datetime.now(UTC))
        subscription.status = 'pending' if flow != 'daily-charge' else 'active'
        subscription.is_trial = flow == 'paid-trial'
        subscription.end_date = datetime.now(UTC) - timedelta(days=2)
        await db.commit()
        if flow == 'pending':
            await activate_pending_subscription(db, user.id, period_days=30, subscription_id=subscription.id)
        elif flow == 'paid-trial':
            await activate_pending_trial_subscription(db, subscription.id, user.id)
        else:
            await update_daily_charge_time(db, subscription)
        await db.refresh(subscription)
        assert subscription.pending_panel_expire_at == subscription.end_date
        assert subscription.end_date > datetime.now(UTC)


@pytest.mark.parametrize('from_daily', [False, True])
async def test_daily_period_switch_keeps_new_expiry_recoverable(postgres_database, monkeypatch, from_daily):
    from app.cabinet.routes.subscription_modules import tariff_switch
    from app.cabinet.schemas.subscription import TariffPurchaseRequest
    from app.database.models import Tariff
    from tests.database.test_multi_tariff_switch_postgres import switch_data

    async with switch_data(postgres_database, monkeypatch) as (db, user):
        source, target = await db.get(Tariff, 1), await db.get(Tariff, 3)
        source.is_daily, target.is_daily = from_daily, not from_daily
        source.daily_price_kopeks = target.daily_price_kopeks = 100
        await db.commit()
        await tariff_switch.switch_tariff(TariffPurchaseRequest(tariff_id=3, period_days=30), user, db, 11)
        subscription = await db.get(Subscription, 11)
        await db.refresh(subscription)
        assert subscription.pending_panel_expire_at == subscription.end_date
        days = 30 if from_daily else 1
        assert abs((subscription.end_date - datetime.now(UTC) - timedelta(days=days)).total_seconds()) < 10
        assert (await db.get(Subscription, 12)).tariff_id == 2
