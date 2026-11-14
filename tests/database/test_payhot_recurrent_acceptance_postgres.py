"""Money and entitlement acceptance use real PostgreSQL locks and receipts."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, PayHotAgreement, PayHotPayment, Subscription, Tariff, Transaction, User
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres
PAYMENT_ID = '00000000-0000-4000-8000-000000000081'
MANDATE_ID = '00000000-0000-4000-8000-000000000082'


async def seed(db):
    end = datetime.now(UTC) + timedelta(days=10)
    user = User(id=1, telegram_id=1, status='active', balance_kopeks=500)
    tariff = Tariff(id=1, name='Example', device_limit=2, traffic_limit_gb=100, period_prices={'30': 10000})
    db.add_all([user, tariff])
    await db.flush()
    target = Subscription(
        id=1,
        user_id=1,
        tariff_id=1,
        is_trial=False,
        status='active',
        end_date=end,
        device_limit=2,
        traffic_limit_gb=100,
        connected_squads=[],
    )
    payment = PayHotPayment(
        user_id=1,
        order_id='00000000-0000-4000-8000-000000000080',
        amount_kopeks=10000,
        currency='RUB',
        payment_method='card',
        payhot_payment_id=PAYMENT_ID,
        status='processing',
        metadata_json={'purpose': 'payhot_recurring'},
    )
    db.add_all([target, payment])
    await db.flush()
    agreement = PayHotAgreement(
        user_id=1,
        owner_id=1,
        subscription_id=1,
        checkout_id='checkout-example',
        first_payment_id=payment.id,
        status='PENDING',
        consent_snapshot={
            'amount_kopeks': 10000,
            'currency': 'RUB',
            'payment_method': 'card',
            'period_days': 30,
            'device_limit': 2,
            'tariff_id': 1,
            'is_daily': False,
            'source_end_date': end.isoformat(),
            'trial_source': None,
            'tariff_terms': {
                'traffic_limit_gb': 100,
                'device_limit': 2,
                'allowed_squads': [],
                'traffic_reset_mode': 'NO_RESET',
            },
        },
    )
    db.add(agreement)
    await db.flush()
    payment.metadata_json = {'purpose': 'payhot_recurring', 'agreement_id': agreement.id}
    await db.commit()
    return payment.id, agreement.id, end


def remote(**changes):
    return dict(
        id=PAYMENT_ID,
        merchant_order_reference='00000000-0000-4000-8000-000000000080',
        amount_minor='10000',
        currency='RUB',
        payment_method='card',
        status='succeeded',
        recurring_enrollment={'status': 'active', 'mandate_id': MANDATE_ID},
        **changes,
    )


async def test_enrollment_success_applies_period_once_not_balance(postgres_database, monkeypatch):
    from app.config import settings
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        pid, aid, end = await seed(db)
        await svc.accept_enrollment_payment(db, pid, remote())
        await svc.accept_enrollment_payment(other, pid, remote())
        target = await db.get(Subscription, 1, populate_existing=True)
        user = await db.get(User, 1, populate_existing=True)
        assert target.end_date == end + timedelta(days=30)
        assert user.balance_kopeks == 500
        assert len((await db.scalars(select(Transaction))).all()) == 1
        agreement = await db.get(PayHotAgreement, aid, populate_existing=True)
        assert agreement.status == 'ACTIVE' and agreement.mandate_id == MANDATE_ID
        assert agreement.next_charge_at == target.end_date


async def test_paid_without_mandate_buys_period_but_cannot_schedule(postgres_database, monkeypatch):
    from app.config import settings
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, end = await seed(db)
        data = remote()
        data['recurring_enrollment'] = {'status': 'processing'}
        await svc.accept_enrollment_payment(db, pid, data)
        assert (await db.get(Subscription, 1)).end_date == end + timedelta(days=30)
        assert (await db.get(PayHotAgreement, aid)).status == 'PENDING'
        assert (await db.get(PayHotAgreement, aid)).next_charge_at is None


async def test_changed_terms_paid_goes_to_balance_without_renewal(postgres_database, monkeypatch):
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=False))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, end = await seed(db)
        await svc.accept_enrollment_payment(db, pid, remote())
        assert (await db.get(User, 1)).balance_kopeks == 10500
        assert (await db.get(Subscription, 1)).end_date == end
        assert (await db.get(PayHotAgreement, aid)).status == 'CANCEL_PENDING'


@pytest.mark.parametrize('cancelled', [False, True])
async def test_repeat_charge_receipt_is_idempotent_even_after_cancellation(postgres_database, monkeypatch, cancelled):
    from app.config import settings
    from app.database.models import PayHotChargeAttempt
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        _, aid, end = await seed(db)
        agreement = await db.get(PayHotAgreement, aid)
        agreement.status = 'CANCEL_PENDING' if cancelled else 'ACTIVE'
        agreement.reason = 'customer' if cancelled else None
        agreement.cancel_requested_at = datetime.now(UTC) if cancelled else None
        agreement.mandate_id = MANDATE_ID
        agreement.next_charge_at = end
        payload = dict(
            mandate_id=MANDATE_ID,
            merchant_recurring_charge_reference='renew-example',
            amount_minor='10000',
            currency='RUB',
            payment_method='card',
        )
        attempt = PayHotChargeAttempt(
            agreement_id=aid,
            cycle_key='cycle-example',
            expected_end_date=end,
            idempotency_key='example-idempotency-key',
            merchant_reference='renew-example',
            request_payload=payload,
            amount_kopeks=10000,
            currency='RUB',
            status='UNKNOWN',
        )
        db.add(attempt)
        await db.commit()
        data = dict(payload, id='00000000-0000-4000-8000-000000000083', status='succeeded')
        await svc.accept_recurring_charge(db, attempt.id, data)
        await svc.accept_recurring_charge(other, attempt.id, data)
        assert (await db.get(Subscription, 1, populate_existing=True)).end_date == end + timedelta(days=30)
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 500
        assert len((await db.scalars(select(Transaction))).all()) == 1
        assert (await db.get(PayHotAgreement, aid, populate_existing=True)).next_charge_at == (
            None if cancelled else end + timedelta(days=30)
        )


async def test_create_timeout_preserves_checkout_and_idempotency(postgres_database, monkeypatch):
    from app.services import payhot_recurrent_service as svc
    from app.services.payhot_service import PayHotAPIError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, _ = await seed(db)
        payment = await db.get(PayHotPayment, pid)
        payment.payhot_payment_id = None
        payment.status = 'creating'
        payment.request_payload = {'merchant_order_reference': payment.order_id}
        await db.commit()
        create = AsyncMock(side_effect=PayHotAPIError(0))
        monkeypatch.setattr(svc.recurrent_api, 'create_enrollment_payment', create)
        for _ in range(2):
            result = await svc.create_payhot_agreement(db, aid)
            assert result['status'] == 'CREATION_UNKNOWN'
        assert create.await_args_list[0].args == create.await_args_list[1].args
        assert (await db.get(PayHotAgreement, aid)).first_payment_id == pid


async def test_late_first_payment_deleted_owner_is_retained_for_review(postgres_database, monkeypatch):
    from sqlalchemy import delete

    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, _ = await seed(db)
        await db.execute(delete(User).where(User.id == 1))
        await db.commit()
        await svc.accept_enrollment_payment(db, pid, remote())
        payment = await db.get(PayHotPayment, pid)
        assert payment.is_paid and payment.transaction_id is None
        assert (await db.get(PayHotAgreement, aid)).status == 'REVIEW'
        assert not (await db.scalars(select(Transaction))).all()


@pytest.mark.parametrize('changed', [False, True])
async def test_worker_reserves_due_cycle_once_and_stops_changed_price(postgres_database, monkeypatch, changed):
    from app.config import settings
    from app.database.models import PayHotChargeAttempt
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(settings, 'PAYHOT_RECURRENT_ENABLED', True)
    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', True)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=not changed))
    monkeypatch.setattr(svc, 'new_charges_enabled', AsyncMock(return_value=True))
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, _ = await seed(db)
        target = await db.get(Subscription, 1)
        target.end_date = datetime.now(UTC) - timedelta(minutes=1)
        agreement = await db.get(PayHotAgreement, aid)
        agreement.status = 'ACTIVE'
        agreement.mandate_id = MANDATE_ID
        agreement.next_charge_at = target.end_date
        payment = await db.get(PayHotPayment, pid)
        payment.is_paid = True
        await db.commit()
        attempt = await svc.prepare_payhot_charge(db, aid)
        if changed:
            assert attempt is None
            assert (await db.get(PayHotAgreement, aid)).status == 'CANCEL_PENDING'
            assert not (await db.scalars(select(PayHotChargeAttempt))).all()
        else:
            assert attempt is not None
            again = await svc.prepare_payhot_charge(db, aid)
            assert again.id == attempt.id
            assert again.idempotency_key == attempt.idempotency_key
            assert again.amount_kopeks == 10000


@pytest.fixture
def reset_panel(monkeypatch):
    from tests.database.test_paid_daily_reset_intent_postgres import panel

    return panel.__wrapped__(monkeypatch)


@pytest.mark.parametrize('failure', [None, 'timeout_no_apply'])
async def test_first_period_delivers_real_reset_intent_before_completing(
    postgres_database, monkeypatch, reset_panel, failure
):
    from app.database.models import PaidTrafficResetIntent
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    reset_panel.failure = failure
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, _ = await seed(db)
        target = await db.get(Subscription, 1)
        target.remnawave_id = 9001
        target.remnawave_short_uuid = 'masked-panel'
        target.traffic_used_gb = 90
        await db.commit()
        await svc.accept_enrollment_payment(db, pid, remote())
        intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
        payment = await db.get(PayHotPayment, pid, populate_existing=True)
        assert reset_panel.reset_calls == 1
        if failure:
            assert intent.state == 'attempted'
            assert payment.fulfillment_state['state'] == 'pending'
        else:
            assert intent.state == 'confirmed'
            assert payment.fulfillment_state['state'] == 'completed'
        await svc.deliver_payhot_renewal(db, aid, payment_id=pid)
        assert reset_panel.reset_calls == 1


async def test_receipt_side_effects_are_emitted_only_for_new_money(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.crud import transaction
    from app.services import payhot_recurrent_service as svc
    from app.services.notification_delivery_service import notification_delivery_service

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    effects = AsyncMock()
    notice = AsyncMock()
    monkeypatch.setattr(transaction, 'emit_transaction_side_effects', effects)
    monkeypatch.setattr(notification_delivery_service, 'notify_autopay_success', notice)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, _, _ = await seed(db)
        await svc.accept_enrollment_payment(db, pid, remote())
        await svc.accept_enrollment_payment(db, pid, remote())
        assert effects.await_count == 1
        assert notice.await_count == 1


async def test_customer_cancel_revokes_without_waiting_for_worker(postgres_database, monkeypatch):
    from app.services import payhot_recurrent_service as svc

    revoke = AsyncMock()
    monkeypatch.setattr(svc, 'revoke_payhot_agreement', revoke)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        _, aid, _ = await seed(db)
        result = await svc.cancel_payhot_agreement(db, user_id=1, subscription_id=1)
        assert result['status'] == 'CANCEL_PENDING'
        revoke.assert_awaited_once_with(db, aid, bot=None)


async def test_delivery_does_not_overwrite_concurrent_refund_review(postgres_database, monkeypatch):
    from app.services import payhot_recurrent_service as svc
    from app.services.subscription_service import SubscriptionService

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        pid, aid, _ = await seed(db)
        payment = await db.get(PayHotPayment, pid)
        payment.is_paid = True
        payment.fulfillment_state = {'kind': 'recurring', 'state': 'pending', 'applied': True}
        await db.commit()

        async def panel(*args, **kwargs):
            from sqlalchemy import update

            await other.execute(
                update(PayHotPayment)
                .where(PayHotPayment.id == pid)
                .values(status='refunded', fulfillment_state={'kind': 'recurring', 'state': 'review', 'applied': True})
            )
            await other.commit()
            return {'uuid': 'masked-panel'}

        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', panel)
        await svc.deliver_payhot_renewal(db, aid, payment_id=pid)
        payment = await db.get(PayHotPayment, pid, populate_existing=True)
        assert payment.fulfillment_state['state'] == 'review'


async def test_refund_blocks_unsubmitted_traffic_reset(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.models import PaidTrafficResetIntent
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(svc, 'terms_match', AsyncMock(return_value=True))
    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, _, _ = await seed(db)
        await svc.accept_enrollment_payment(db, pid, remote())
        await svc.accept_enrollment_payment(db, pid, {**remote(), 'status': 'refunded'})
        intent = (await db.scalars(select(PaidTrafficResetIntent))).one()
        assert intent.state == 'blocked' and intent.last_error == 'refund_review'


async def test_unpaid_enrollment_refund_status_survives_stale_success(postgres_database, monkeypatch):
    from app.services import payhot_recurrent_service as svc

    monkeypatch.setattr(svc, 'deliver_payhot_renewal', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        pid, aid, _ = await seed(db)
        await svc.accept_enrollment_payment(db, pid, {**remote(), 'status': 'refunded'})
        await svc.accept_enrollment_payment(db, pid, remote())
        payment = await db.get(PayHotPayment, pid, populate_existing=True)
        assert payment.status == 'refunded' and payment.is_paid is False
        assert payment.fulfillment_state['state'] == 'review'
        assert (await db.get(PayHotAgreement, aid)).status == 'REVIEW'
        assert not (await db.scalars(select(Transaction))).all()
