"""Post-credit crash/replay boundaries on real PostgreSQL, no external services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Base, PayHotPayment, User
from app.services.payment.payhot_fulfillment import fulfill_payhot_payment
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def seed(db):
    db.add(User(id=1, telegram_id=1, balance_kopeks=10000))
    await db.flush()
    payment = PayHotPayment(
        user_id=1,
        order_id='synthetic-1',
        amount_kopeks=10000,
        currency='RUB',
        status='succeeded',
        is_paid=True,
        fulfillment_state={'kind': 'topup', 'target_id': 1, 'state': 'pending'},
    )
    db.add(payment)
    await db.commit()
    return payment.id


async def test_legacy_commits_and_receipt_are_atomic_and_concurrent_once(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as sessions:
        pid = await seed(sessions[0])

        async def referral(db, user_id, amount, bot=None, notify=True):
            user = await db.get(User, user_id)
            user.balance_kopeks += 123
            await db.commit()
            user.balance_kopeks += 456
            await db.commit()
            return True

        monkeypatch.setattr('app.services.referral_service.process_referral_topup', referral)
        cart = AsyncMock(return_value=False)
        monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', cart)
        await asyncio.gather(*(fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid) for db in sessions))
        await sessions[0].rollback()
        assert (await sessions[0].get(User, 1, populate_existing=True)).balance_kopeks == 10579
        assert (await sessions[0].get(PayHotPayment, pid, populate_existing=True)).fulfillment_state['state'] == 'done'
        assert cart.await_count == 1


@pytest.mark.parametrize('swallowed', [False, True])
async def test_referral_partial_commit_rolls_back_and_retry_has_no_duplicate(postgres_database, monkeypatch, swallowed):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as (db,):
        pid = await seed(db)

        async def broken(stage, user_id, amount, bot=None, notify=True):
            user = await stage.get(User, user_id)
            user.balance_kopeks += 123
            await stage.commit()
            if swallowed:
                await stage.rollback()
                return True
            raise RuntimeError('synthetic crash')

        monkeypatch.setattr('app.services.referral_service.process_referral_topup', broken)
        cart = AsyncMock(return_value=False)
        monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', cart)
        with pytest.raises(RuntimeError):
            await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10000
        assert (await db.get(PayHotPayment, pid, populate_existing=True)).fulfillment_state['state'] == 'pending'
        cart.assert_not_called()

        async def recovered(stage, user_id, amount, bot=None, notify=True):
            user = await stage.get(User, user_id)
            user.balance_kopeks += 123
            await stage.commit()
            return True

        monkeypatch.setattr('app.services.referral_service.process_referral_topup', recovered)
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10123
        assert cart.await_count == 1


async def test_cart_crash_after_debit_never_automatically_repeats(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as (db,):
        pid = await seed(db)
        monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock(return_value=True))

        async def cart(user, amount, session, bot):
            user.balance_kopeks -= 500
            await session.commit()
            raise RuntimeError('response lost after debit')

        action = AsyncMock(side_effect=cart)
        monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', action)
        with pytest.raises(RuntimeError):
            await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        await db.rollback()
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 9500
        assert (await db.get(PayHotPayment, pid, populate_existing=True)).fulfillment_state['state'] == 'attention'
        assert action.await_count == 1


async def test_unpaid_invoice_never_runs_financial_fulfillment(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as (db,):
        pid = await seed(db)
        payment = await db.get(PayHotPayment, pid)
        payment.is_paid = False
        await db.commit()
        referral = AsyncMock(return_value=True)
        monkeypatch.setattr('app.services.referral_service.process_referral_topup', referral)
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        referral.assert_not_called()
        assert await db.scalar(select(User.balance_kopeks).where(User.id == 1)) == 10000


async def test_actual_referral_processor_commission_and_receipt_once(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.models import ReferralEarning

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as (db,):
        pid = await seed(db)
        db.add(User(id=2, telegram_id=2, balance_kopeks=0))
        await db.flush()
        user = await db.get(User, 1)
        user.referred_by_id = 2
        user.has_made_first_topup = True
        await db.commit()
        monkeypatch.setattr(type(settings), 'is_referral_levels_scheme', lambda self: False)
        monkeypatch.setattr(
            'app.services.referral_service.calculate_referral_commission_percent', AsyncMock(return_value=10)
        )
        monkeypatch.setattr('app.services.referral_service._is_commission_limit_reached', AsyncMock(return_value=False))
        monkeypatch.setattr(
            'app.services.payment.common.send_cart_notification_after_topup', AsyncMock(return_value=False)
        )

        committed_observations = []

        async def send_after_commit(*args, **kwargs):
            async with AsyncSession(db.bind) as observer:
                assert (await observer.get(User, 2)).balance_kopeks == 1000
                assert (await observer.get(PayHotPayment, pid)).fulfillment_state['referral'] == 'done'
                committed_observations.append(True)

        notification = AsyncMock(side_effect=send_after_commit)
        monkeypatch.setattr('app.services.referral_service.send_referral_notification', notification)
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        assert (await db.get(User, 2, populate_existing=True)).balance_kopeks == 1000
        earnings = list((await db.scalars(select(ReferralEarning).where(ReferralEarning.user_id == 2))).all())
        assert len(earnings) == 1
        assert earnings[0].amount_kopeks == 1000
        assert committed_observations == [True]
        assert notification.await_count == 1
        assert notification.call_args.kwargs['bonus_kopeks'] == 1000


@pytest.mark.parametrize('language', ['ru', 'en', 'zh', 'fa', 'ua'])
async def test_days_notice_is_localized_and_sent_after_atomic_receipt(postgres_database, monkeypatch, language):
    from app.database.models import ReferralEarning

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as (db,):
        pid = await seed(db)
        user = await db.get(User, 1)
        user.language = language
        await db.commit()
        notice = AsyncMock()
        monkeypatch.setattr('app.services.referral_service.send_referral_notification', notice)

        async def award(stage, user_id, amount, bot=None):
            assert bot is None
            notice.assert_not_called()
            stage.add(
                ReferralEarning(
                    user_id=user_id,
                    referral_id=user_id,
                    amount_kopeks=0,
                    reason='days_bonus',
                    reward_type='days',
                    days_granted=3,
                    level=1,
                )
            )
            await stage.commit()
            notice.assert_not_called()
            return True

        monkeypatch.setattr('app.services.referral_service.process_referral_topup', award)
        monkeypatch.setattr(
            'app.services.payment.common.send_cart_notification_after_topup', AsyncMock(return_value=False)
        )
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        assert notice.await_count == 1
        assert notice.call_args.kwargs['bonus_days'] == 3
        assert notice.call_args.kwargs['bonus_kopeks'] == 0
        text = notice.call_args.args[2]
        assert 'REFERRAL_' not in text
        assert '3' in text


async def test_owner_merge_between_snapshot_and_locks_defers_to_new_owner(postgres_database, monkeypatch):
    from app.services.payment.payhot_fulfillment import _StageSession

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as (db,):
        pid = await seed(db)
        db.add(User(id=2, telegram_id=2, balance_kopeks=10000))
        await db.commit()
        original_scalar = _StageSession.scalar
        merged = False

        async def interleaved_scalar(stage, statement, *args, **kwargs):
            nonlocal merged
            value = await original_scalar(stage, statement, *args, **kwargs)
            if not merged:
                merged = True
                async with AsyncSession(db.bind, expire_on_commit=False) as merger:
                    invoice = await merger.get(PayHotPayment, pid)
                    invoice.user_id = 2
                    invoice.fulfillment_state = {**invoice.fulfillment_state, 'target_id': 2}
                    await merger.commit()
            return value

        monkeypatch.setattr(_StageSession, 'scalar', interleaved_scalar)
        referral = AsyncMock(return_value=True)
        cart = AsyncMock(return_value=False)
        monkeypatch.setattr('app.services.referral_service.process_referral_topup', referral)
        monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', cart)
        with pytest.raises(RuntimeError, match='owner changed'):
            await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        referral.assert_not_called()
        cart.assert_not_called()
        await fulfill_payhot_payment(db, SimpleNamespace(bot=None), pid)
        assert referral.await_count == 1
        assert referral.call_args.args[1] == 2
        assert cart.call_args.args[0].id == 2
