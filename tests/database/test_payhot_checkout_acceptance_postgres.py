"""Real checkout snapshot must survive canonical acceptance without repricing mocks."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, PayHotAgreement, PayHotPayment, Subscription, Transaction, User
from app.services import payhot_recurrent_checkout as checkout, payhot_recurrent_service as runtime
from tests.database.test_payhot_recurrent_checkout_postgres import (  # noqa: F401 -- imported pytest fixture
    seed,
    setup_policy,
)
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('mode', ['new', 'existing', 'trial', 'daily'])
async def test_real_quote_to_first_paid_period(postgres_database, monkeypatch, mode):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(runtime, 'deliver_payhot_renewal', AsyncMock())
    from app.services import payhot_recurrent_notifications

    monkeypatch.setattr(payhot_recurrent_notifications, 'emit_payhot_receipt', AsyncMock())

    async def provider(payload, key, *, config):
        return {
            'payment': {
                'id': str(uuid4()),
                'merchant_order_reference': payload['merchant_order_reference'],
                'amount_minor': payload['amount_minor'],
                'currency': 'RUB',
                'payment_method': payload['payment_method'],
                'status': 'requires_action',
            },
            'checkout': {'url': 'https://checkout.example/payment'},
        }

    monkeypatch.setattr(runtime.recurrent_api, 'create_enrollment_payment', provider)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, source = await seed(db, trial=mode == 'trial', daily=mode == 'daily')
        if mode == 'new':
            source.status = 'expired'
            source.end_date = datetime.now(UTC) - timedelta(days=1)
            await db.commit()
        q = await checkout.quote_payhot_purchase(
            db,
            user=user,
            tariff_id=1,
            period_days=1 if mode == 'daily' else 6,
            subscription_id=None if mode == 'new' else source.id,
            channel='cabinet',
        )
        result = await checkout.purchase_payhot_quote(
            db, user=user, quote_id=q['quote_id'], checkout_id=str(uuid4()), channel='cabinet'
        )
        agreement = await db.get(PayHotAgreement, result['id'])
        payment = await db.get(PayHotPayment, agreement.first_payment_id)
        remote = {
            'id': payment.payhot_payment_id,
            'merchant_order_reference': payment.order_id,
            'amount_minor': str(payment.amount_kopeks),
            'currency': 'RUB',
            'payment_method': payment.payment_method,
            'status': 'succeeded',
            'recurring_enrollment': {'status': 'active', 'mandate_id': str(uuid4())},
        }
        await runtime.accept_enrollment_payment(db, payment.id, remote)
        await runtime.accept_enrollment_payment(db, payment.id, remote)
        agreement = await db.get(PayHotAgreement, agreement.id, populate_existing=True)
        target = await db.get(Subscription, agreement.subscription_id, populate_existing=True)
        assert agreement.status == 'ACTIVE', (agreement.reason, payment.fulfillment_state)
        assert target.is_trial is False
        assert target.tariff_id == 1 and target.end_date > datetime.now(UTC)
        assert (await db.get(User, user.id)).balance_kopeks == 0
        assert len((await db.scalars(select(Transaction))).all()) == 1
        # Move the paid boundary to the next scheduled cycle; keep actual
        # consent/composition and real revalidation for the repeat charge.
        target.end_date = datetime.now(UTC) - timedelta(seconds=1)
        agreement.next_charge_at = target.end_date
        await db.commit()
        attempt = await runtime.prepare_payhot_charge(db, agreement.id)
        assert attempt is not None, agreement.reason
        charge_id = str(uuid4())
        canonical = {**attempt.request_payload, 'id': charge_id, 'status': 'succeeded'}
        create_charge = AsyncMock(return_value={'recurring_charge': {**canonical, 'status': 'processing'}})
        monkeypatch.setattr(runtime.recurrent_api, 'create_recurring_charge', create_charge)
        monkeypatch.setattr(
            runtime.recurrent_api, 'get_recurring_charge', AsyncMock(return_value={'recurring_charge': canonical})
        )
        await runtime.submit_payhot_charge(db, attempt.id)
        await runtime.submit_payhot_charge(db, attempt.id)
        assert create_charge.await_count == 1
        assert len((await db.scalars(select(Transaction))).all()) == 2
        assert (await db.get(User, user.id)).balance_kopeks == 0
        assert (await db.get(Subscription, target.id, populate_existing=True)).end_date > datetime.now(UTC)
