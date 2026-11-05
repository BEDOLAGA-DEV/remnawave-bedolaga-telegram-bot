"""Invoice acceptance and account merge use a compatible lock order."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import AnorePayment, Base, Transaction, User
from app.services.account_merge_service import execute_merge
from app.services.payment.anore import AnorePaymentMixin
from tests.database.test_anore_payments_postgres import payload, seed
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('merge_first', [False, True])
async def test_acceptance_and_merge_converge_without_losing_money(postgres_database, monkeypatch, merge_first):
    service = AnorePaymentMixin()
    monkeypatch.setattr(service, '_after_anore_payment', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        db,
        merge_db,
        check,
    ):
        await seed(db)
        db.add(User(id=2, email='primary@example.com', balance_kopeks=200))
        await db.commit()
        merged = asyncio.Event()

        async def merge():
            await execute_merge(merge_db, primary_user_id=2, secondary_user_id=1)
            await merge_db.commit()
            merged.set()

        async def accept():
            try:
                await service.process_anore_callback(db, payload())
            except RuntimeError as exc:
                assert 'owner changed' in str(exc)
                await merged.wait()
                # Equivalent to provider retrying the unacknowledged event.
                await service.process_anore_callback(db, payload())

        tasks = [merge(), accept()] if merge_first else [accept(), merge()]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
        assert (await check.get(User, 2)).balance_kopeks == 10701
        assert (await check.get(User, 1)).balance_kopeks == 0
        payments = (await check.scalars(select(AnorePayment))).all()
        transactions = (await check.scalars(select(Transaction))).all()
        assert len(payments) == len(transactions) == 1
        assert payments[0].user_id == transactions[0].user_id == 2
        assert payments[0].transaction_id == transactions[0].id
