"""Classic checkout commits balance, subscription rights and ledger together."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud import transaction as transaction_crud, user as user_crud
from app.database.models import Base, ServerSquad, Subscription, SubscriptionServer, Transaction, User
from app.services import (
    monitoring_service as monitoring,
    remnawave_retry_queue as retry,
    subscription_purchase_service as purchase,
)
from tests.database.test_renewal_survives_stale_panel_snapshot_postgres import BALANCE, PRICE, _seed
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_session, postgres_sessions


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture(autouse=True)
def isolated_purchase(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr(retry, 'remnawave_retry_queue', retry.RemnaWaveRetryQueue())
    monkeypatch.setattr(monitoring.cache, 'exists', AsyncMock(return_value=True))
    panel = SimpleNamespace(
        update_remnawave_user=AsyncMock(), create_remnawave_user=AsyncMock(), disable_remnawave_user=AsyncMock()
    )
    monkeypatch.setattr(purchase, 'SubscriptionService', lambda: panel)
    return panel


def _pricing(*, promo=False):
    return SimpleNamespace(
        final_total=PRICE,
        base_original_total=PRICE,
        promo_discount_value=100 if promo else 0,
        promo_discount_percent=10 if promo else 0,
        server_ids=[],
        server_prices_for_period=[],
        months=1,
        selection=SimpleNamespace(period=SimpleNamespace(days=30), traffic_value=200, devices=4, servers=['s1']),
    )


async def _prepare(db, *, promo=False):
    user, subscription, old_end = await _seed(db, datetime.now(UTC))
    if promo:
        user.promo_offer_discount_percent = 10
        user.promo_offer_discount_source = 'test'
        user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1)
        await db.commit()
    user = await user_crud.lock_user_for_pricing(db, user.id)
    return SimpleNamespace(user=user, subscription=subscription, payload={}), _pricing(promo=promo), old_end


@pytest.mark.parametrize('fault', ['after-charge', 'ledger', 'commit'])
async def test_failure_restores_balance_promo_rights_and_ledger(postgres_database, monkeypatch, fault):
    async with postgres_session(postgres_database, TABLES) as db:
        context, pricing, old_end = await _prepare(db, promo=True)
        user, subscription = context.user, context.subscription
        if fault == 'after-charge':
            charge = purchase.subtract_user_balance

            async def fail_after_charge(*args, **kwargs):
                await charge(*args, **kwargs)
                raise RuntimeError('synthetic checkout failure')

            monkeypatch.setattr(purchase, 'subtract_user_balance', fail_after_charge)
        elif fault == 'ledger':
            monkeypatch.setattr(
                purchase, 'create_transaction', AsyncMock(side_effect=RuntimeError('synthetic checkout failure'))
            )
        else:
            commit = db.commit

            async def fail_rights_commit():
                # Fail the commit containing changed rights, not the old standalone debit.
                if subscription.end_date != old_end:
                    raise RuntimeError('synthetic checkout failure')
                await commit()

            monkeypatch.setattr(db, 'commit', fail_rights_commit)
        with pytest.raises(RuntimeError, match='synthetic checkout failure'):
            await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
        await db.rollback()  # Request teardown cannot undo an earlier committed debit.
        await db.refresh(user)
        await db.refresh(subscription)
        assert user.balance_kopeks == BALANCE
        assert user.promo_offer_discount_percent == 10
        assert user.has_had_paid_subscription is False
        assert subscription.end_date == old_end
        assert subscription.pending_panel_expire_at is None
        assert (subscription.traffic_limit_gb, subscription.device_limit) == (150, 3)
        assert list(await db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('kind', ['new', 'trial'])
async def test_failed_purchase_preserves_new_or_trial_subscription(postgres_database, monkeypatch, kind):
    async with postgres_session(postgres_database, TABLES) as db:
        context, pricing, old_end = await _prepare(db)
        user, subscription = context.user, context.subscription
        if kind == 'new':
            await db.delete(subscription)
            context.subscription = None
        else:
            subscription.is_trial = True
        await db.commit()
        user = await user_crud.lock_user_for_pricing(db, user.id)
        monkeypatch.setattr(
            purchase, 'create_transaction', AsyncMock(side_effect=RuntimeError('synthetic ledger failure'))
        )
        with pytest.raises(RuntimeError, match='synthetic ledger failure'):
            await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
        await db.rollback()
        await db.refresh(user)
        assert user.balance_kopeks == BALANCE
        subscriptions = list(await db.scalars(select(Subscription)))
        if kind == 'new':
            assert subscriptions == []
        else:
            assert len(subscriptions) == 1
            assert subscriptions[0].is_trial is True
            assert subscriptions[0].end_date == old_end
        assert list(await db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('stage', ['after-charge', 'ledger'])
async def test_cancellation_rolls_back_checkout_and_releases_locks(postgres_database, monkeypatch, stage):
    async with postgres_sessions(postgres_database, TABLES) as (db, observer):
        context, pricing, old_end = await _prepare(db)
        uid, sid = context.user.id, context.subscription.id
        pending = asyncio.Event()
        charge = purchase.subtract_user_balance

        async def pause(*args, **kwargs):
            if stage == 'after-charge':
                await charge(*args, **kwargs)
            pending.set()
            await asyncio.Future()

        monkeypatch.setattr(
            purchase, 'subtract_user_balance' if stage == 'after-charge' else 'create_transaction', pause
        )
        task = asyncio.create_task(purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing))
        try:
            await asyncio.wait_for(pending.wait(), 10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        current = await observer.get(User, uid)
        subscription = await observer.get(Subscription, sid)
        assert current.balance_kopeks == BALANCE
        assert subscription.end_date == old_end
        assert list(await observer.scalars(select(Transaction))) == []
        assert not db.in_transaction(), 'Cancelled checkout must release its locks without relying on HTTP teardown'


async def test_in_flight_purchase_blocks_autopay_for_the_same_due_cycle(
    postgres_database, monkeypatch, isolated_purchase
):
    async with postgres_sessions(postgres_database, TABLES, count=3) as (manual, automatic, observer):
        context, pricing, old_end = await _prepare(manual)
        uid, sid = context.user.id, context.subscription.id
        deducted, proceed = asyncio.Event(), asyncio.Event()
        charge = purchase.subtract_user_balance

        async def pause_after_charge(*args, **kwargs):
            result = await charge(*args, **kwargs)
            deducted.set()
            await asyncio.wait_for(proceed.wait(), 10)
            return result

        monkeypatch.setattr(purchase, 'subtract_user_balance', pause_after_charge)

        async def submit():
            await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(manual, context, pricing)
            await manual.commit()

        worker = monitoring.MonitoringService(bot=None)
        worker.subscription_service = isolated_purchase
        first = asyncio.create_task(submit())
        second = None
        try:
            await asyncio.wait_for(deducted.wait(), 10)
            second = asyncio.create_task(worker._process_autopayments(automatic))
            await lock_waiter_appeared(observer, timeout=1)
        finally:
            proceed.set()
            await asyncio.wait_for(asyncio.gather(*([first, second] if second else [first])), 15)
        await observer.rollback()
        user = await observer.get(User, uid)
        subscription = await observer.get(Subscription, sid)
        assert user.balance_kopeks == BALANCE - PRICE
        assert subscription.end_date == old_end + timedelta(days=30)
        assert len(list(await observer.scalars(select(Transaction)))) == 1


@pytest.mark.parametrize('failure', ['exception', 'none'])
async def test_panel_rollback_keeps_committed_payment_and_rights(
    postgres_database, monkeypatch, isolated_purchase, failure
):
    async with postgres_sessions(postgres_database, TABLES) as (db, observer):
        context, pricing, old_end = await _prepare(db)
        uid, sid = context.user.id, context.subscription.id
        snapshots = []

        async def panel_failure(session, subscription, **kwargs):
            current = await observer.get(User, uid)
            saved = await observer.get(Subscription, sid)
            snapshots.append(
                (current.balance_kopeks, saved.end_date, len(list(await observer.scalars(select(Transaction)))))
            )
            await session.rollback()
            if failure == 'exception':
                raise TimeoutError('synthetic panel failure')

        isolated_purchase.update_remnawave_user.side_effect = panel_failure
        result = await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
        assert snapshots == [(BALANCE - PRICE, old_end + timedelta(days=30), 1)]
        assert result['transaction'].id
        await db.refresh(context.subscription)
        assert context.subscription.pending_panel_expire_at == old_end + timedelta(days=30)
        assert retry.remnawave_retry_queue.pending_count == 1
        item = retry.remnawave_retry_queue._queue[0]
        assert item.action == 'update'
        assert item.reset_traffic is True
        assert item.reset_reason == 'miniapp purchase'


@pytest.mark.parametrize('stage', ['conversion', 'servers', 'events'])
async def test_auxiliary_rollback_does_not_report_paid_purchase_as_failure(postgres_database, monkeypatch, stage):
    async with postgres_session(postgres_database, TABLES) as db:
        context, pricing, old_end = await _prepare(db)
        if stage == 'conversion':
            context.subscription.is_trial = True
            await db.commit()
        elif stage == 'servers':
            pricing.server_ids = [1]
            db.add(
                Subscription(
                    user_id=context.user.id,
                    is_trial=True,
                    status='trial',
                    start_date=datetime.now(UTC),
                    end_date=datetime.now(UTC) + timedelta(days=2),
                    connected_squads=[],
                )
            )
            await db.commit()

        async def failure(*args, **kwargs):
            await db.rollback()
            raise RuntimeError('synthetic postcommit failure')

        module = transaction_crud if stage == 'events' else purchase
        function = {
            'conversion': 'create_subscription_conversion',
            'servers': 'add_subscription_servers',
            'events': 'emit_transaction_side_effects',
        }[stage]
        monkeypatch.setattr(module, function, failure)
        result = await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
        await db.refresh(context.user)
        await db.refresh(result['subscription'])
        assert context.user.balance_kopeks == BALANCE - PRICE
        assert result['subscription'].is_trial is False
        assert result['subscription'].end_date >= datetime.now(UTC) + timedelta(days=29)
        assert result['transaction'].id
        assert len(list(await db.scalars(select(Transaction)))) == 1
        if stage == 'servers':
            assert not list(await db.scalars(select(Subscription).where(Subscription.is_trial.is_(True))))


async def test_insufficient_cabinet_purchase_still_saves_cart_and_returns_402(postgres_database, monkeypatch):
    from fastapi import HTTPException

    from app.cabinet.routes.subscription_modules import purchase as cabinet

    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    async with postgres_session(postgres_database, TABLES) as db:
        context, pricing, old_end = await _prepare(db)
        context.user.balance_kopeks = 0
        await db.commit()
        uid = context.user.id
        monkeypatch.setattr(cabinet.purchase_service, 'build_options', AsyncMock(return_value=context))
        monkeypatch.setattr(cabinet.purchase_service, 'parse_selection', lambda *args: pricing.selection)
        monkeypatch.setattr(cabinet.purchase_service, 'calculate_pricing', AsyncMock(return_value=pricing))
        saved = {}

        async def save_cart(user_id, cart):
            saved[user_id] = cart

        monkeypatch.setattr(cabinet.user_cart_service, 'save_user_cart', save_cart)
        with pytest.raises(HTTPException) as failure:
            await cabinet.submit_purchase(
                cabinet.PurchasePreviewRequest(selection={'period_days': 30, 'traffic_value': 200, 'devices': 4}),
                context.user,
                db,
            )
        assert failure.value.status_code == 402
        assert saved[uid]['return_to_cart'] is True
        await db.refresh(context.user)
        await db.refresh(context.subscription)
        assert context.user.balance_kopeks == 0
        assert context.subscription.end_date == old_end


@pytest.mark.parametrize('failure', ['exception', 'none'])
async def test_panel_failure_keeps_server_counters_and_trial_decrements(
    postgres_database, monkeypatch, isolated_purchase, failure
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with postgres_sessions(postgres_database, TABLES) as (db, observer):
        context, pricing, old_end = await _prepare(db)
        paid_server = ServerSquad(squad_uuid='paid-squad', display_name='Paid', current_users=0)
        trial_server = ServerSquad(squad_uuid='trial-squad', display_name='Trial', current_users=1)
        trial = Subscription(
            user_id=context.user.id,
            is_trial=True,
            status='trial',
            start_date=datetime.now(UTC),
            end_date=datetime.now(UTC) + timedelta(days=2),
            connected_squads=['trial-squad'],
            remnawave_id=777,
        )
        db.add_all([paid_server, trial_server, trial])
        await db.commit()
        pricing.server_ids = [paid_server.id]
        pricing.server_prices_for_period = [0]
        pricing.selection.servers = ['paid-squad']
        counters_at_panel = []

        async def record_counters(*args, **kwargs):
            counts = await observer.execute(select(ServerSquad.squad_uuid, ServerSquad.current_users))
            counters_at_panel.append(dict(counts.all()))
            await observer.rollback()

        async def fail_panel(*args, **kwargs):
            await record_counters()
            # No internal rollback: checkout must itself preserve flushed counters.
            if failure == 'exception':
                raise TimeoutError('synthetic panel failure')

        isolated_purchase.disable_remnawave_user.side_effect = record_counters
        isolated_purchase.update_remnawave_user.side_effect = fail_panel
        result = await purchase.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
        await observer.rollback()
        counts = dict((await observer.execute(select(ServerSquad.squad_uuid, ServerSquad.current_users))).all())
        expected = {'paid-squad': 1, 'trial-squad': 0}
        assert counts == expected
        assert counters_at_panel == [expected, expected]
        links = list(await observer.scalars(select(SubscriptionServer)))
        assert len(links) == 1
        assert links[0].subscription_id == result['subscription'].id
        assert len(list(await observer.scalars(select(Transaction)))) == 1
