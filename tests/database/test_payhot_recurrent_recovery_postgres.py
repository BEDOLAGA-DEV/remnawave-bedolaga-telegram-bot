"""Terminal financial recovery is GET-only and uses real receipt acceptance."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, PayHotAgreement, PayHotPayment, Subscription, Transaction
from tests.database.test_payhot_recurrent_acceptance_postgres import remote, seed
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
async def test_terminal_first_payment_late_success_get_only(postgres_database, monkeypatch, status):
    from app.services import payhot_recurrent_service as svc
    from app.services.payhot_recurrent_recovery import recover_terminal_payhot_receipts

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    monkeypatch.setattr(svc, '_config', lambda agreement: object())
    get = AsyncMock(return_value={'payment': remote()})
    post = AsyncMock(side_effect=AssertionError('must never create a payment'))
    monkeypatch.setattr(svc.payhot_service, 'get_payment', get)
    monkeypatch.setattr(svc, 'create_payhot_agreement', post)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, end = await seed(db)
        payment = await db.get(PayHotPayment, pid)
        payment.status = status
        payment.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        agreement = await db.get(PayHotAgreement, aid)
        agreement.status = 'FAILED'
        await db.commit()
        assert (await recover_terminal_payhot_receipts(db)) == {'inspected': 1, 'errors': 0}
        await recover_terminal_payhot_receipts(db)
        assert (await db.get(Subscription, 1, populate_existing=True)).end_date == end + timedelta(days=30)
        assert len((await db.scalars(select(Transaction))).all()) == 1
        assert get.await_count == 1
        post.assert_not_awaited()


async def test_missing_remote_id_and_old_terminal_are_not_recreated(postgres_database, monkeypatch):
    from app.services import payhot_recurrent_service as svc
    from app.services.payhot_recurrent_recovery import recover_terminal_payhot_receipts

    get = AsyncMock(side_effect=AssertionError('no remote read expected'))
    monkeypatch.setattr(svc.payhot_service, 'get_payment', get)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, _, _ = await seed(db)
        payment = await db.get(PayHotPayment, pid)
        payment.status = 'failed'
        payment.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        payment.payhot_payment_id = None
        await db.commit()
        assert (await recover_terminal_payhot_receipts(db))['inspected'] == 0
        payment.payhot_payment_id = remote()['id']
        payment.created_at = datetime.now(UTC) - timedelta(days=8)
        await db.commit()
        assert (await recover_terminal_payhot_receipts(db))['inspected'] == 0
        get.assert_not_awaited()


@pytest.mark.parametrize('status', ['FAILED', 'CANCELLED'])
async def test_terminal_charge_success_after_revocation_get_only(postgres_database, monkeypatch, status):
    from app.database.models import PayHotChargeAttempt
    from app.services import payhot_recurrent_service as svc
    from app.services.payhot_recurrent_recovery import recover_terminal_payhot_receipts
    from tests.database.test_payhot_recurrent_acceptance_postgres import MANDATE_ID

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    monkeypatch.setattr(svc, '_config', lambda agreement: object())
    post = AsyncMock(side_effect=AssertionError('no new charge'))
    monkeypatch.setattr(svc.recurrent_api, 'create_recurring_charge', post)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        _, aid, end = await seed(db)
        agreement = await db.get(PayHotAgreement, aid)
        agreement.status = 'CANCELLED'
        agreement.cancel_requested_at = datetime.now(UTC)
        agreement.mandate_id = MANDATE_ID
        data = dict(
            id='00000000-0000-4000-8000-000000000083',
            mandate_id=MANDATE_ID,
            merchant_recurring_charge_reference='renew-example',
            amount_minor='10000',
            currency='RUB',
            payment_method='card',
            status='succeeded',
        )
        attempt = PayHotChargeAttempt(
            agreement_id=aid,
            cycle_key='cycle-example',
            expected_end_date=end,
            idempotency_key='example-idempotency-key',
            merchant_reference='renew-example',
            request_payload={},
            amount_kopeks=10000,
            currency='RUB',
            status=status,
            provider_charge_id=data['id'],
            updated_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        db.add(attempt)
        await db.commit()
        get = AsyncMock(return_value={'recurring_charge': data})
        monkeypatch.setattr(svc.recurrent_api, 'get_recurring_charge', get)
        assert (await recover_terminal_payhot_receipts(db)) == {'inspected': 1, 'errors': 0}
        await recover_terminal_payhot_receipts(db)
        assert (await db.get(Subscription, 1, populate_existing=True)).end_date == end + timedelta(days=30)
        assert len((await db.scalars(select(Transaction))).all()) == 1
        assert (await db.get(PayHotAgreement, aid)).next_charge_at is None
        assert get.await_count == 1
        post.assert_not_awaited()


async def test_terminal_read_timeout_keeps_receipt_and_backs_off(postgres_database, monkeypatch):
    from app.services import payhot_recurrent_service as svc
    from app.services.payhot_recurrent_recovery import recover_terminal_payhot_receipts

    monkeypatch.setattr(svc, '_config', lambda agreement: object())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, _, end = await seed(db)
        payment = await db.get(PayHotPayment, pid)
        payment.status = 'failed'
        payment.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        await db.commit()

        async def unavailable(*args, **kwargs):
            assert not db.in_transaction()
            raise TimeoutError

        get = AsyncMock(side_effect=unavailable)
        monkeypatch.setattr(svc.payhot_service, 'get_payment', get)
        assert await recover_terminal_payhot_receipts(db) == {'inspected': 1, 'errors': 1}
        assert await recover_terminal_payhot_receipts(db) == {'inspected': 0, 'errors': 0}
        assert (await db.get(PayHotPayment, pid, populate_existing=True)).status == 'failed'
        assert (await db.get(Subscription, 1, populate_existing=True)).end_date == end
        assert not (await db.scalars(select(Transaction))).all()
        assert get.await_count == 1
