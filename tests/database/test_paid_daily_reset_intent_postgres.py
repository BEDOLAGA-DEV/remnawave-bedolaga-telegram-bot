"""TG030: paid reset delivery survives failure without replaying destructive POSTs."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import Base, PaidTrafficResetIntent, Subscription, Transaction, User
from app.services import (
    paid_traffic_reset_service as resets,
    subscription_auto_purchase_service as auto,
    subscription_renewal_service as renewal,
    subscription_service as sync_module,
)
from app.services.daily_subscription_service import DailySubscriptionService
from tests.fixtures.postgres_db import postgres_engine
from tests.services.test_daily_charge_traffic_reset import _subscription, _tariff, _user
from tests.services.test_daily_topup_resume_traffic_reset import _rows


pytestmark = pytest.mark.postgres
GIB = 1024**3


class Panel:
    def __init__(self):
        self.user = SimpleNamespace(
            id=9001,
            short_uuid='masked-panel',
            status='ACTIVE',
            last_traffic_reset_at=datetime.now(UTC) - timedelta(days=2),
            last_traffic_reset_at_known=True,
            user_traffic=object(),
            used_traffic_bytes=90 * GIB,
            traffic_limit_bytes=100 * GIB,
        )
        self.reset_calls = 0
        self.enable_calls = 0
        self.canonical_calls = []
        self.failure = None
        self.enable_failure = False
        self.before_reset = None

    async def get_user_by_id(self, panel_id):
        assert panel_id == 9001
        return self.user

    async def reset_user_traffic(self, panel_id, *, enrich=True):
        assert panel_id == 9001 and enrich is False
        self.reset_calls += 1
        if self.before_reset:
            await self.before_reset()
        if self.failure in ('timeout_no_apply', 'cancel'):
            if self.failure == 'cancel':
                import asyncio

                raise asyncio.CancelledError
            raise TimeoutError('test reset response unknown')
        self.user.last_traffic_reset_at = datetime.now(UTC)
        self.user.used_traffic_bytes = 0
        if self.failure == 'lost_ack':
            self.user.used_traffic_bytes = 7 * GIB
            raise TimeoutError('test reset applied but response lost')
        return self.user

    async def enable_user(self, panel_id):
        self.enable_calls += 1
        if self.enable_failure:
            raise TimeoutError('test enable failed')
        self.user.status = 'ACTIVE'
        return self.user


@pytest.fixture
def panel(monkeypatch):
    panel = Panel()
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'DAILY_SUBSCRIPTIONS_ENABLED', True)
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())

    class Service:
        is_configured = True

        @asynccontextmanager
        async def get_api_client(self):
            yield panel

        async def update_remnawave_user(self, db, sub, **kwargs):
            panel.canonical_calls.append(kwargs)
            assert kwargs['reset_traffic'] is False
            if panel.failure == 'canonical_none':
                return None
            if panel.failure == 'canonical_raise':
                raise TimeoutError('test canonical failure')
            return panel.user

        create_remnawave_user = update_remnawave_user
        sync_remnawave_user = update_remnawave_user

    monkeypatch.setattr(sync_module, 'SubscriptionService', Service)
    monkeypatch.setattr(auto, 'SubscriptionService', Service)
    return panel


async def charge(db, flow, *, strategy='NO_RESET'):
    db.add_all(
        [_user(), _tariff(strategy), _subscription()]
        if flow == 'scheduled'
        else _rows(traffic_reset_mode=strategy, status='disabled')
    )
    await db.commit()
    svc = DailySubscriptionService()
    sub = await svc._reload_daily_subscription(db, 10)
    result = (
        await svc._process_single_charge(db, sub)
        if flow == 'scheduled'
        else await auto.try_resume_disabled_daily_after_topup(db, await db.get(User, 1))
    )
    assert result == ('charged' if flow == 'scheduled' else True)
    return (await db.scalars(select(PaidTrafficResetIntent))).one_or_none()


async def due(maker):
    async with maker() as db:
        await db.execute(update(PaidTrafficResetIntent).values(next_check_at=None))
        await db.commit()


@pytest.mark.parametrize('flow', ['scheduled', 'topup'])
@pytest.mark.parametrize('failure', ['canonical_none', 'canonical_raise', 'lost_ack', 'timeout_no_apply', None])
@pytest.mark.parametrize('multi', [False, True])
async def test_delivery_failure_and_restart(postgres_database, monkeypatch, panel, flow, failure, multi):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    panel.failure = failure
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            intent = await charge(db, flow)
            await db.refresh(intent)
            assert (await db.get(User, 1)).balance_kopeks == 99000
            assert len(list((await db.scalars(select(Transaction))).all())) == 1
            expected = (
                'pending' if failure and failure.startswith('canonical') else 'attempted' if failure else 'confirmed'
            )
            assert intent.state == expected
            await db.refresh(await db.get(Subscription, 10))
            assert (await db.get(Subscription, 10)).traffic_used_gb == (0 if failure is None else 90)
        panel.failure = None
        await due(maker)
        # A new invocation has no in-memory intent/queue state.
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
            assert (await db.get(User, 1)).balance_kopeks == 99000
            assert len(list((await db.scalars(select(Transaction))).all())) == 1
            if failure == 'timeout_no_apply':
                assert intent.state == 'attempted'
                assert (await db.get(Subscription, 10)).traffic_used_gb == 90
            else:
                assert intent.state == 'confirmed'
                assert (await db.get(Subscription, 10)).traffic_used_gb == (7 if failure == 'lost_ack' else 0)
            assert panel.reset_calls == 1


async def test_confirmed_reset_retries_only_limited_activation(postgres_database, panel):
    panel.user.status = 'LIMITED'
    panel.enable_failure = True
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            intent = await charge(db, 'topup')
            await db.refresh(intent)
            assert intent.state == 'confirmed' and intent.activation_pending
        panel.enable_failure = False
        panel.user.used_traffic_bytes = 7 * GIB
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
            assert intent.state == 'confirmed' and not intent.activation_pending
        assert panel.reset_calls == 1
        assert panel.enable_calls == 2
        assert panel.user.used_traffic_bytes == 7 * GIB


async def test_attempt_is_durable_before_external_post(postgres_database, panel):
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

        async def observer():
            async with maker() as db:
                intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
                assert intent.state == 'attempted' and intent.target_panel_id == 9001
                assert (await db.get(User, 1)).balance_kopeks == 99000

        panel.before_reset = observer
        async with maker() as db:
            await charge(db, 'scheduled')
        assert panel.reset_calls == 1


@pytest.mark.parametrize('known', [False, True])
async def test_unknown_outcome_never_blindly_retries(postgres_database, panel, known):
    panel.failure = 'timeout_no_apply'
    panel.user.last_traffic_reset_at_known = known
    if not known:
        panel.user.last_traffic_reset_at = None
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            await charge(db, 'scheduled')
        panel.failure = None
        for _ in range(resets.MAX_CHECKS):
            await due(maker)
            await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
            assert intent.state == 'blocked' and intent.last_error == 'reset_outcome_ambiguous'
        assert panel.reset_calls == 1


async def test_late_marker_resolves_blocked_without_another_reset(postgres_database, panel):
    panel.failure = 'timeout_no_apply'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            await charge(db, 'scheduled')
        async with maker() as db:
            await db.execute(update(PaidTrafficResetIntent).values(checks=resets.MAX_CHECKS))
            await db.commit()
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
            assert intent.state == 'blocked'
            assert intent.next_check_at > datetime.now(UTC) + timedelta(minutes=50)
        panel.user.last_traffic_reset_at = datetime.now(UTC)
        panel.user.used_traffic_bytes = 2 * GIB
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            assert (await db.scalars(select(PaidTrafficResetIntent.state))).one() == 'confirmed'
            assert (await db.get(Subscription, 10)).traffic_used_gb == 2
        assert panel.reset_calls == 1


@pytest.mark.parametrize('used,expected_enables', [(2, 1), (7, 0)])
async def test_late_ack_does_not_enable_an_exhausted_new_quota(postgres_database, panel, used, expected_enables):
    panel.failure = 'lost_ack'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            await charge(db, 'scheduled')
        panel.user.used_traffic_bytes = used * GIB
        panel.user.traffic_limit_bytes = 5 * GIB
        panel.user.status = 'LIMITED'
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
            assert intent.state == 'confirmed' and not intent.activation_pending
            assert (await db.get(Subscription, 10)).traffic_used_gb == used
        assert panel.reset_calls == 1 and panel.enable_calls == expected_enables


@pytest.mark.parametrize('invalid', ['wrong_id', 'empty_short', 'missing_short'])
async def test_invalid_panel_identity_rejected_before_reset(postgres_database, panel, invalid):
    if invalid == 'wrong_id':
        panel.user.id = 9002
    else:
        panel.user.short_uuid = '' if invalid == 'empty_short' else None
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            intent = await charge(db, 'scheduled')
            await db.refresh(intent)
            assert intent.state == 'blocked'
            assert intent.attempted_at is None
        assert panel.reset_calls == 0


@pytest.mark.parametrize('change', ['short', 'id', 'cycle', 'owner'])
async def test_identity_and_cycle_rechecked_after_durable_claim(postgres_database, monkeypatch, panel, change):
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        original = resets._locked
        calls = 0

        async def race(db, intent_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                async with maker() as other:
                    sub = await other.get(Subscription, 10)
                    if change == 'short':
                        sub.remnawave_short_uuid = 'replacement'
                    elif change == 'id':
                        sub.remnawave_id = 9002
                    elif change == 'cycle':
                        sub.last_daily_charge_at += timedelta(seconds=1)
                    else:
                        other.add(User(id=2, status='active'))
                        await other.flush()
                        sub.user_id = 2
                    await other.commit()
            return await original(db, intent_id)

        monkeypatch.setattr(resets, '_locked', race)
        async with maker() as db:
            intent = await charge(db, 'scheduled')
            await db.refresh(intent)
            assert intent.state == 'blocked' and intent.attempted_at is None
        assert panel.reset_calls == 0


async def test_old_ack_never_changes_replacement_cycle(postgres_database, panel):
    panel.failure = 'lost_ack'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            await charge(db, 'scheduled')
        async with maker() as db:
            sub = await db.get(Subscription, 10)
            sub.last_daily_charge_at += timedelta(seconds=1)
            sub.remnawave_id = 9002
            sub.traffic_used_gb = 23
            await db.commit()
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
            assert intent.state == 'confirmed' and not intent.activation_pending
            assert (await db.get(Subscription, 10)).traffic_used_gb == 23
            assert (await db.get(Subscription, 10)).remnawave_id == 9002
        assert panel.reset_calls == 1


async def test_two_workers_send_one_reset(postgres_database, panel):
    import asyncio

    from tests.fixtures.postgres_db import wait_for_lock_waiter

    panel.failure = 'canonical_none'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            intent_id = (await charge(db, 'scheduled')).id
        panel.failure = None
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold():
            entered.set()
            await release.wait()

        panel.before_reset = hold
        async with maker() as first, maker() as second, maker() as observer:
            task1 = asyncio.create_task(resets.deliver_daily_reset(first, intent_id))
            await asyncio.wait_for(entered.wait(), 5)
            task2 = asyncio.create_task(resets.deliver_daily_reset(second, intent_id))
            try:
                await wait_for_lock_waiter(observer)
            finally:
                release.set()
            assert await task1
            assert await task2
        assert panel.reset_calls == 1


@pytest.mark.parametrize('flow', ['scheduled', 'topup'])
async def test_intent_failure_rolls_back_whole_charge(postgres_database, monkeypatch, panel, flow):
    monkeypatch.setattr(resets, 'stage_daily_reset', AsyncMock(side_effect=RuntimeError('test intent write failed')))
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            db.add_all(
                [_user(), _tariff('NO_RESET'), _subscription()]
                if flow == 'scheduled'
                else _rows(traffic_reset_mode='NO_RESET', status='disabled')
            )
            await db.commit()
            svc = DailySubscriptionService()
            sub = await svc._reload_daily_subscription(db, 10)
            result = (
                await svc._process_single_charge(db, sub)
                if flow == 'scheduled'
                else await auto.try_resume_disabled_daily_after_topup(db, await db.get(User, 1))
            )
            assert result == ('error' if flow == 'scheduled' else False)
            assert (await db.get(User, 1)).balance_kopeks == 100000
            assert list((await db.scalars(select(Transaction))).all()) == []
            assert list((await db.scalars(select(PaidTrafficResetIntent))).all()) == []
        assert panel.reset_calls == 0


async def test_cancel_after_dispatch_leaves_durable_attempt(postgres_database, panel):
    import asyncio

    panel.failure = 'cancel'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            with pytest.raises(asyncio.CancelledError):
                await charge(db, 'scheduled')
        async with maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 99000
            assert (await db.scalars(select(PaidTrafficResetIntent.state))).one() == 'attempted'
        panel.failure = None
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        assert panel.reset_calls == 1


async def test_target_claim_unique_index_serializes_different_intents(postgres_database, panel):
    """Different subscription locks cannot bypass the global destructive-target claim."""
    import asyncio

    from sqlalchemy.exc import IntegrityError

    from tests.fixtures.postgres_db import wait_for_lock_waiter

    panel.failure = 'canonical_none'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            first_id = (await charge(db, 'scheduled')).id
            db.add(User(id=2, status='active'))
            await db.flush()
            second_sub = Subscription(
                id=20,
                user_id=2,
                status='active',
                is_trial=False,
                end_date=datetime.now(UTC) + timedelta(days=1),
                last_daily_charge_at=datetime.now(UTC),
            )
            db.add(second_sub)
            await db.flush()
            second_id = await resets.stage_daily_reset(db, second_sub, SimpleNamespace(id=None), reason='test')
            await db.commit()
        async with maker() as first, maker() as second, maker() as observer:
            for db, intent_id in [(first, first_id), (second, second_id)]:
                intent = await db.get(PaidTrafficResetIntent, intent_id)
                intent.target_panel_id = 9001
                intent.target_short_uuid = 'masked-panel'
                intent.attempted_at = datetime.now(UTC)
                intent.state = 'attempted'
            await first.flush()
            second_commit = asyncio.create_task(second.commit())
            await wait_for_lock_waiter(observer)
            await first.commit()
            with pytest.raises(IntegrityError):
                await second_commit
            await second.rollback()
        async with maker() as db:
            assert (await db.get(PaidTrafficResetIntent, first_id)).state == 'attempted'
            assert (await db.get(PaidTrafficResetIntent, second_id)).state == 'pending'
        assert panel.reset_calls == 0  # Neither test transaction dispatched before claim commit.


async def test_pending_current_cycle_is_covered_by_late_older_reset(postgres_database, panel):
    panel.failure = 'timeout_no_apply'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            old_id = (await charge(db, 'scheduled')).id
            sub = await db.get(Subscription, 10)
            sub.last_daily_charge_at += timedelta(seconds=1)
            new_id = await resets.stage_daily_reset(db, sub, SimpleNamespace(id=None), reason='next paid cycle')
            await db.commit()
            assert not await resets.deliver_daily_reset(db, new_id)
            await db.refresh(await db.get(PaidTrafficResetIntent, new_id))
            assert (await db.get(PaidTrafficResetIntent, new_id)).baseline_observed
        # The first response eventually takes effect after both financial commits.
        panel.user.last_traffic_reset_at = datetime.now(UTC)
        panel.user.used_traffic_bytes = 7 * GIB
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            assert (await db.get(PaidTrafficResetIntent, old_id)).state == 'confirmed'
            assert (await db.get(PaidTrafficResetIntent, new_id)).state == 'confirmed'
            assert (await db.get(Subscription, 10)).traffic_used_gb == 7
        assert panel.reset_calls == 1


async def test_foreign_subscription_owner_prevents_reset(postgres_database, monkeypatch, panel):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    panel.failure = 'canonical_none'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            intent_id = (await charge(db, 'scheduled')).id
            (await db.get(Subscription, 10)).remnawave_id = None
            db.add(User(id=2, status='active'))
            await db.flush()
            db.add(
                Subscription(
                    id=20,
                    user_id=2,
                    status='active',
                    is_trial=False,
                    remnawave_id=9001,
                    end_date=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await db.commit()
            assert not await resets.deliver_daily_reset(db, intent_id)
            await db.refresh(await db.get(PaidTrafficResetIntent, intent_id))
            assert (await db.get(PaidTrafficResetIntent, intent_id)).last_error == 'panel_account_owned_elsewhere'
        assert panel.reset_calls == 0


@pytest.mark.parametrize('flow', ['scheduled', 'topup'])
async def test_repeated_paid_callback_has_one_intent_and_one_reset(postgres_database, panel, flow):
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            await charge(db, flow)
            svc = DailySubscriptionService()
            sub = await svc._reload_daily_subscription(db, 10)
            result = (
                await svc._process_single_charge(db, sub)
                if flow == 'scheduled'
                else await auto.try_resume_disabled_daily_after_topup(db, await db.get(User, 1))
            )
            assert result == ('skipped' if flow == 'scheduled' else False)
            assert len(list((await db.scalars(select(PaidTrafficResetIntent))).all())) == 1
            assert len(list((await db.scalars(select(Transaction))).all())) == 1
            assert (await db.get(User, 1)).balance_kopeks == 99000
        assert panel.reset_calls == 1


async def test_matching_direct_ack_can_confirm_without_reset_marker(postgres_database, panel):
    panel.user.last_traffic_reset_at = None
    panel.user.last_traffic_reset_at_known = False

    async def reset_without_marker(panel_id, *, enrich=True):
        assert panel_id == 9001 and not enrich
        panel.reset_calls += 1
        panel.user.used_traffic_bytes = 0
        return panel.user

    panel.reset_user_traffic = reset_without_marker
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            intent = await charge(db, 'scheduled')
            await db.refresh(intent)
            assert intent.state == 'confirmed'
            assert intent.confirmed_last_reset_at is None
            assert (await db.get(Subscription, 10)).traffic_used_gb == 0
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        assert panel.reset_calls == 1


@pytest.mark.parametrize('flow', ['scheduled', 'topup'])
@pytest.mark.parametrize('reset,strategy', [(False, 'NO_RESET'), (True, 'DAY')])
async def test_existing_policy_can_charge_without_reset_intent(
    postgres_database, monkeypatch, panel, flow, reset, strategy
):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset)
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            assert await charge(db, flow, strategy=strategy) is None
            assert (await db.get(User, 1)).balance_kopeks == 99000
            assert (await db.get(Subscription, 10)).traffic_used_gb == 90
        assert panel.reset_calls == 0


@pytest.mark.parametrize('flow', ['scheduled', 'topup'])
async def test_fully_discounted_daily_charge_has_unique_durable_intent(postgres_database, monkeypatch, panel, flow):
    from app.services.pricing_engine import PricingEngine

    group = SimpleNamespace(get_discount_percent=lambda *args: 100)
    monkeypatch.setattr(PricingEngine, 'resolve_promo_group', staticmethod(lambda user: group))
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            intent = await charge(db, flow)
            await db.refresh(intent)
            assert intent.state == 'confirmed' and intent.transaction_id is not None
            assert (await db.get(User, 1)).balance_kopeks == 100000
            assert (await db.scalars(select(Transaction.amount_kopeks))).one() == 0
        assert panel.reset_calls == 1


@pytest.mark.parametrize('cancelled', [False, True])
async def test_known_unsent_claim_can_resume_without_duplicate(postgres_database, panel, cancelled):
    import asyncio

    original_get = panel.get_user_by_id
    reads = 0

    async def failing_second_get(panel_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            if cancelled:
                raise asyncio.CancelledError
            raise TimeoutError('pre-POST read failed')
        return await original_get(panel_id)

    panel.get_user_by_id = failing_second_get
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await charge(db, 'scheduled')
            else:
                await charge(db, 'scheduled')
        async with maker() as db:
            intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
            assert intent.state == 'pending'
            assert intent.attempted_at is None
            assert (await db.get(User, 1)).balance_kopeks == 99000
        assert panel.reset_calls == 0
        await due(maker)
        await resets.process_pending_daily_resets(maker)
        async with maker() as db:
            assert (await db.scalars(select(PaidTrafficResetIntent.state))).one() == 'confirmed'
            assert (await db.get(User, 1)).balance_kopeks == 99000
        assert panel.reset_calls == 1
