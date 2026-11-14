"""Account merges preserve pending PayHot delivery ownership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, PayHotPayment, User
from app.services.account_merge_service import execute_merge
from app.services.payment.payhot_fulfillment import fulfill_payhot_payment
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def test_merge_transfers_pending_topup_delivery_but_not_guest_purchase_target(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, check):
        db.add_all(
            [
                User(id=1, telegram_id=1, balance_kopeks=10000),
                User(id=2, email='primary@example.com', balance_kopeks=200),
            ]
        )
        await db.flush()
        for order_id, kind in [('topup-example', 'topup'), ('gift-example', 'guest')]:
            db.add(
                PayHotPayment(
                    user_id=1,
                    order_id=order_id,
                    amount_kopeks=10000,
                    currency='RUB',
                    status='succeeded',
                    is_paid=True,
                    fulfillment_state={'kind': kind, 'target_id': 1, 'state': 'pending'},
                )
            )
        await db.commit()
        await execute_merge(db, primary_user_id=2, secondary_user_id=1)
        await db.commit()
        topup = await check.scalar(select(PayHotPayment).where(PayHotPayment.order_id == 'topup-example'))
        gift = await check.scalar(select(PayHotPayment).where(PayHotPayment.order_id == 'gift-example'))
        assert topup.user_id == 2 and topup.fulfillment_state['target_id'] == 2
        assert gift.fulfillment_state['target_id'] == 1
        referral = AsyncMock(return_value=True)
        monkeypatch.setattr('app.services.referral_service.process_referral_topup', referral)
        monkeypatch.setattr(
            'app.services.payment.common.send_cart_notification_after_topup', AsyncMock(return_value=False)
        )
        await fulfill_payhot_payment(check, SimpleNamespace(bot=None), topup.id)
        assert referral.call_args.args[1] == 2
        assert (await check.get(User, 2, populate_existing=True)).balance_kopeks == 10200


@pytest.mark.parametrize('merge_first', [False, True])
async def test_payhot_acceptance_and_merge_retry_keep_one_credit(postgres_database, monkeypatch, merge_first):
    import asyncio

    from app.database.models import Transaction
    from app.services.payment.payhot import PayHotPaymentMixin
    from tests.database.test_payhot_payments_postgres import remote, seed

    service = PayHotPaymentMixin()
    monkeypatch.setattr(service, '_fulfill_payhot', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        db,
        merge_db,
        check,
    ):
        pid = await seed(db)
        db.add(User(id=2, email='primary@example.com', balance_kopeks=200))
        await db.commit()
        merged = asyncio.Event()

        async def merge():
            await execute_merge(merge_db, primary_user_id=2, secondary_user_id=1)
            await merge_db.commit()
            merged.set()

        async def accept():
            try:
                await service._accept_payhot_state(db, pid, remote())
            except RuntimeError as exc:
                assert 'owner changed' in str(exc)
                await merged.wait()
                await service._accept_payhot_state(db, pid, remote())

        tasks = [merge(), accept()] if merge_first else [accept(), merge()]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        payment = await check.get(PayHotPayment, pid)
        assert payment.user_id == payment.fulfillment_state['target_id'] == 2
        assert (await check.get(User, 2)).balance_kopeks == 10700
        assert len((await check.scalars(select(Transaction))).all()) == 1
