"""PayHot financial invariants on independent PostgreSQL connections."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, select

from app.database.models import Base, PayHotPayment, Transaction, User
from app.services.payhot_service import payhot_service
from app.services.payment.payhot import PayHotPaymentMixin
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres
REMOTE = '00000000-0000-4000-8000-000000000001'


def remote(**changes):
    return {
        'id': REMOTE,
        'merchant_order_reference': 'order-1',
        'amount_minor': '10000',
        'currency': 'RUB',
        'payment_method': 'card',
        'status': 'succeeded',
        **changes,
    }


async def seed(db, **changes):
    db.add(User(id=1, telegram_id=1, balance_kopeks=500))
    await db.flush()
    payment = PayHotPayment(
        user_id=1,
        order_id='order-1',
        amount_kopeks=10000,
        currency='RUB',
        payment_method='card',
        payhot_payment_id=REMOTE,
        **changes,
    )
    db.add(payment)
    await db.commit()
    return payment.id


@pytest.fixture
def service(monkeypatch):
    service = PayHotPaymentMixin()
    monkeypatch.setattr(service, '_fulfill_payhot', AsyncMock())
    return service


@pytest.mark.parametrize(
    'fees',
    [
        None,
        {
            'principal_minor': '10000',
            'payer_fee_minor': '500',
            'merchant_fee_minor': '0',
            'merchant_net_minor': '10000',
        },
        {'principal_minor': '10000', 'payer_fee_minor': '0', 'merchant_fee_minor': '500', 'merchant_net_minor': '9500'},
        {
            'principal_minor': '10000',
            'payer_fee_minor': '250',
            'merchant_fee_minor': '250',
            'merchant_net_minor': '9750',
        },
        {'principal_minor': '10000', 'payer_fee_minor': '0', 'merchant_fee_minor': '0', 'merchant_net_minor': '10000'},
    ],
)
async def test_concurrent_acceptance_credits_only_principal_once(postgres_database, service, fees):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as sessions:
        local_id = await seed(sessions[0])
        await asyncio.gather(*(service._accept_payhot_state(db, local_id, remote(fees=fees)) for db in sessions))
        db = sessions[0]
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10500
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == 10000
        payment = await db.get(PayHotPayment, local_id, populate_existing=True)
        assert payment.is_paid and payment.fulfillment_state['state'] == 'pending'


@pytest.mark.parametrize(
    'changes',
    [
        {'currency': 'EUR'},
        {'amount_minor': '10500'},
        {'fees': {'principal_minor': '9900'}},
        {'payment_method': 'sbp'},
        {'merchant_order_reference': 'other'},
        {'id': '00000000-0000-4000-8000-000000000002'},
        {'environment': 'sandbox'},
        {'origin': 'recurring'},
        {'recurring_charge_id': '00000000-0000-4000-8000-000000000002'},
        {'merchant_recurring_charge_reference': 'renew-example'},
    ],
)
async def test_mismatch_cannot_credit(postgres_database, service, changes):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        local_id = await seed(db)
        with pytest.raises(ValueError):
            await service._accept_payhot_state(db, local_id, remote(**changes))
        assert (await other.get(User, 1)).balance_kopeks == 500
        assert not (await other.scalars(select(Transaction))).all()


async def test_test_environment_is_rejected_without_money_or_delivery(postgres_database, service):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        local_id = await seed(db)
        with pytest.raises(ValueError, match='environment mismatch'):
            await service._accept_payhot_state(db, local_id, remote(environment='sandbox'))
        payment = await db.get(PayHotPayment, local_id)
        assert payment.status == 'creating' and not payment.is_paid and payment.transaction_id is None
        assert (await db.get(User, 1)).balance_kopeks == 500
        service._fulfill_payhot.assert_not_awaited()


@pytest.mark.parametrize(
    'status',
    [
        'requires_action',
        'processing',
        'authorized',
        'partially_captured',
        'failed',
        'cancelled',
        'manual_reconciliation',
    ],
)
async def test_non_success_no_credit_then_late_success(postgres_database, service, status):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        local_id = await seed(db)
        await service._accept_payhot_state(db, local_id, remote(status=status))
        assert (await db.get(User, 1)).balance_kopeks == 500
        await service._accept_payhot_state(db, local_id, remote())
        await service._accept_payhot_state(db, local_id, remote(status='failed'))
        assert (await db.get(User, 1)).balance_kopeks == 10500
        assert (await db.get(PayHotPayment, local_id)).status == 'succeeded'


async def test_webhook_uses_canonical_state_not_claimed_success(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        get = AsyncMock(return_value={'payment': remote(status='processing')})
        monkeypatch.setattr(payhot_service, 'get_payment', get)
        assert await service.process_payhot_callback(
            db, {'event_type': 'payment.succeeded', 'data': remote(), 'event_id': 'event-1'}
        )
        assert (await db.get(User, 1)).balance_kopeks == 500
        get.assert_awaited_once()


async def test_creation_unknown_response_recovers_same_idempotency_without_fake_email(
    postgres_database, service, monkeypatch
):
    from app.config import settings
    from app.services.payhot_service import PayHotAPIError

    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', True)
    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'phk_v2_example')
    monkeypatch.setattr(settings, 'PAYHOT_WEBHOOK_SECRET', 'whsec_v2_example')
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add(User(id=1, telegram_id=1, balance_kopeks=500))
        await db.commit()
        calls = []

        async def create(payload, key, *, config):
            calls.append((payload, key))
            if len(calls) == 1:
                raise PayHotAPIError(503)
            return {'payment': remote(merchant_order_reference=key, status='requires_action')}

        monkeypatch.setattr(payhot_service, 'create_payment', create)
        reissue = AsyncMock(return_value={'checkout': {'url': 'https://checkout.example/session#secret'}})
        monkeypatch.setattr(payhot_service, 'reissue_checkout', reissue)
        assert (
            await service.create_payhot_payment(db, user_id=1, amount_kopeks=10000, payment_method_type='card') is None
        )
        payment = (await db.scalars(select(PayHotPayment))).one()
        local_id, order_id = payment.id, payment.order_id
        monkeypatch.setattr(
            payhot_service,
            'get_payment',
            AsyncMock(return_value={'payment': remote(merchant_order_reference=order_id)}),
        )
        result = await service.check_payhot_payment_status(db, local_id)
        assert result['is_paid']
        assert calls[0] == calls[1]
        assert 'email' not in calls[0][0]['customer']
        assert (await db.get(PayHotPayment, local_id)).payment_url.endswith('#secret')
        assert (await db.get(User, 1)).balance_kopeks == 10500


@pytest.mark.parametrize('is_gift', [False, True])
async def test_guest_email_is_real_contact_not_recipient(postgres_database, service, monkeypatch, is_gift):
    from app.config import settings
    from app.database.models import GuestPurchase

    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', True)
    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'phk_v2_example')
    monkeypatch.setattr(settings, 'PAYHOT_WEBHOOK_SECRET', 'whsec_v2_example')
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add(
            GuestPurchase(
                token='purchase-example',
                is_gift=is_gift,
                gift_recipient_type='email' if is_gift else None,
                gift_recipient_value='recipient@example.com' if is_gift else None,
                contact_type='email',
                contact_value='buyer@example.com',
                amount_kopeks=10000,
                currency='RUB',
                period_days=30,
                payment_method='payhot_card',
                status='pending',
            )
        )
        await db.commit()
        create = AsyncMock(side_effect=Exception('stop before network'))
        monkeypatch.setattr(payhot_service, 'create_payment', create)
        with pytest.raises(Exception, match='stop before network'):
            await service.create_payhot_payment(
                db,
                user_id=None,
                amount_kopeks=10000,
                payment_method_type='card',
                metadata={'purchase_token': 'purchase-example'},
            )
        assert create.call_args.args[0]['customer']['email'] == 'buyer@example.com'


@pytest.mark.parametrize('wallet', ['applepay', 'googlepay'])
async def test_card_checkout_can_finish_with_wallet(postgres_database, service, wallet):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        local_id = await seed(db)
        await service._accept_payhot_state(db, local_id, remote(payment_method=wallet))
        assert (await db.get(User, 1)).balance_kopeks == 10500


async def test_concurrent_recovery_reissues_checkout_only_once(postgres_database, service, monkeypatch):
    from app.services.payhot_service import payhot_service

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        local_id = await seed(db, request_payload={'merchant_order_reference': 'order-1'})
        create = AsyncMock(
            return_value={'payment': remote(status='requires_action', expires_at='2030-01-01T00:00:00Z')}
        )
        reissue = AsyncMock(return_value={'checkout': {'url': 'https://checkout.example/session#secret'}})
        monkeypatch.setattr(payhot_service, 'create_payment', create)
        monkeypatch.setattr(payhot_service, 'reissue_checkout', reissue)
        first, second = await db.get(PayHotPayment, local_id), await other.get(PayHotPayment, local_id)
        await asyncio.gather(
            service._recover_payhot_creation(db, first, config=payhot_service.snapshot()),
            service._recover_payhot_creation(other, second, config=payhot_service.snapshot()),
        )
        reissue.assert_awaited_once()
        assert (await db.get(PayHotPayment, local_id)).expires_at.year == 2030


async def test_lost_creation_already_paid_recovers_without_reissuing_checkout(postgres_database, service, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        local_id = await seed(db, request_payload={'merchant_order_reference': 'order-1'})
        payment = await db.get(PayHotPayment, local_id)
        payment.payhot_payment_id = None
        await db.commit()
        monkeypatch.setattr(payhot_service, 'create_payment', AsyncMock(return_value={'payment': remote()}))
        monkeypatch.setattr(payhot_service, 'get_payment', AsyncMock(return_value={'payment': remote()}))
        reissue = AsyncMock(side_effect=ValueError('terminal payment has no checkout'))
        monkeypatch.setattr(payhot_service, 'reissue_checkout', reissue)
        result = await service.check_payhot_payment_status(db, local_id)
        assert result['is_paid']
        reissue.assert_not_awaited()


@pytest.mark.parametrize('restricted', [False, True])
async def test_authenticated_gift_policy_uses_buyer_not_recipient(postgres_database, service, monkeypatch, restricted):
    from app.database.models import GuestPurchase
    from app.services import payment_method_config_service as policies

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add_all(
            [
                User(id=1, email='buyer@example.com', restriction_topup=restricted),
                User(id=2, email='recipient@example.com'),
            ]
        )
        await db.flush()
        db.add(
            GuestPurchase(
                token='gift-example',
                buyer_user_id=1,
                user_id=2,
                is_gift=True,
                contact_type='email',
                contact_value='buyer@example.com',
                amount_kopeks=10000,
                currency='RUB',
                period_days=30,
                payment_method='payhot_card',
                status='pending',
            )
        )
        await db.commit()
        policy = AsyncMock(return_value={'min_amount_kopeks': 1, 'max_amount_kopeks': 100000, 'options': ['card']})
        monkeypatch.setattr(policies, 'get_payhot_checkout_policy', policy)
        create = AsyncMock(side_effect=ValueError('no transport in this test'))
        monkeypatch.setattr(payhot_service, 'create_payment', create)
        await service.create_payhot_payment(
            db,
            user_id=None,
            amount_kopeks=10000,
            metadata={'purchase_token': 'gift-example'},
            payment_method_type='card',
        )
        if restricted:
            create.assert_not_awaited()
            policy.assert_not_awaited()
        else:
            assert inspect(policy.call_args.args[1]).identity == (1,)
            assert create.call_args.args[0]['customer'] == {'id': 'user_1', 'email': 'buyer@example.com'}


async def test_over_integer_limit_rejected_before_database_or_provider(service):
    db = AsyncMock()
    assert await service.create_payhot_payment(db, user_id=1, amount_kopeks=2147483648) is None
    db.get.assert_not_awaited()


@pytest.mark.parametrize('refund_status', ['refunded', 'partially_refunded'])
@pytest.mark.parametrize('previously_paid', [False, True])
async def test_refund_tombstone_blocks_stale_success_and_fulfillment(
    postgres_database, service, refund_status, previously_paid
):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        local_id = await seed(db)
        if previously_paid:
            await service._accept_payhot_state(db, local_id, remote())
            service._fulfill_payhot.reset_mock()
        await service._accept_payhot_state(db, local_id, remote(status=refund_status))
        await service._accept_payhot_state(db, local_id, remote())
        payment = await db.get(PayHotPayment, local_id, populate_existing=True)
        assert payment.status == refund_status
        assert payment.fulfillment_state['state'] == 'attention'
        assert payment.fulfillment_state['reason'] == 'refund_review'
        assert payment.is_paid == previously_paid
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == (10500 if previously_paid else 500)
        assert len((await db.scalars(select(Transaction))).all()) == int(previously_paid)
        service._fulfill_payhot.assert_not_awaited()


@pytest.mark.parametrize('refund_status', ['refunded', 'partially_refunded'])
async def test_refund_tombstone_blocks_manual_fulfillment_retry(postgres_database, monkeypatch, refund_status):
    from app.services.payment import payhot_fulfillment

    service = PayHotPaymentMixin()
    fulfill = AsyncMock()
    monkeypatch.setattr(payhot_fulfillment, 'fulfill_payhot_payment', fulfill)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        local_id = await seed(
            db,
            is_paid=True,
            status=refund_status,
            fulfillment_state={'kind': 'topup', 'target_id': 1, 'state': 'attention', 'reason': 'refund_review'},
        )
        await service.check_payhot_payment_status(db, local_id)
        fulfill.assert_not_awaited()


async def test_refund_committing_while_delivery_waits_prevents_new_stage(postgres_database, service, monkeypatch):
    from app.services import referral_service
    from app.services.payment import payhot_fulfillment as fulfillment
    from tests.fixtures.postgres_db import wait_for_lock_waiter

    referral = AsyncMock(return_value=True)
    monkeypatch.setattr(referral_service, 'process_referral_topup', referral)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (
        db,
        worker,
        observer,
    ):
        local_id = await seed(
            db,
            is_paid=True,
            status='succeeded',
            fulfillment_state={'kind': 'topup', 'target_id': 1, 'state': 'pending'},
        )
        await db.scalar(select(User).where(User.id == 1).with_for_update())
        task = asyncio.create_task(fulfillment._referral_stage(worker, local_id))
        try:
            await wait_for_lock_waiter(observer)
            await service._accept_payhot_state(db, local_id, remote(status='refunded'))
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        referral.assert_not_awaited()
        assert await fulfillment._claim(worker, local_id, 'cart') is None
        await fulfillment._complete(worker, local_id, 'cart')
        payment = await db.get(PayHotPayment, local_id, populate_existing=True)
        assert payment.fulfillment_state['state'] == 'attention'
        assert payment.fulfillment_state['reason'] == 'refund_review'
