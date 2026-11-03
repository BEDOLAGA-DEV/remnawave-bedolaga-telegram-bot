import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, Subscription, Tariff, User
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres
REMOTE = '00000000-0000-4000-8000-000000000002'
CHARGE = '00000000-0000-4000-8000-000000000003'
NOW = datetime.now(UTC)


def payment(**overrides):
    return (
        dict(
            id=CHARGE,
            subscription_id=REMOTE,
            store_id='shop',
            payment_method='SBP',
            status='PAID',
            amount=10000,
            currency='RUB',
            is_sandbox=False,
            paid_at=NOW.isoformat(),
        )
        | overrides
    )


async def seed(db):
    from app.database.models import CisPaySubscription
    from app.services.cispay_recurrent import cispay_tariff_terms

    db.add(User(id=1, telegram_id=1, status='active', balance_kopeks=0))
    db.add(Tariff(id=1, name='Test', device_limit=2, period_prices={'30': 10000}, traffic_limit_gb=0))
    await db.flush()
    db.add(
        Subscription(
            id=1,
            user_id=1,
            tariff_id=1,
            status='active',
            is_trial=False,
            device_limit=2,
            traffic_limit_gb=0,
            start_date=NOW,
            end_date=NOW + timedelta(days=5),
        )
    )
    await db.flush()
    db.add(
        CisPaySubscription(
            id=1,
            user_id=1,
            owner_id=1,
            subscription_id=1,
            shop_id='shop',
            provider_id=REMOTE,
            checkout_id='00000000-0000-4000-8000-000000000001',
            status='ACTIVE',
            purpose='Test',
            snapshot={
                'tariff_id': 1,
                'device_limit': 2,
                'period_days': 30,
                'amount_kopeks': 10000,
                'tariff_terms': cispay_tariff_terms(await db.get(Tariff, 1)),
            },
        )
    )
    await db.commit()


async def test_duplicate_parallel_paid_grants_once(postgres_database, monkeypatch):
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        await seed(db)
        await asyncio.gather(
            service.process_cispay_subscription_charge(db, payment()),
            service.process_cispay_subscription_charge(other, payment()),
        )
        await db.refresh(await db.get(Subscription, 1))
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=35)
        assert (await db.get(User, 1)).balance_kopeks == 0


@pytest.mark.parametrize(
    'overrides',
    [
        {'is_sandbox': True},
        {'is_sandbox': None},
        {'amount': 1},
        {'currency': 'USD'},
        {'store_id': 'another-shop'},
        {'payment_method': 'CARD'},
    ],
)
async def test_invalid_charge_cannot_grant(postgres_database, monkeypatch, overrides):
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        try:
            await service.process_cispay_subscription_charge(db, payment(**overrides))
        except ValueError:
            await db.rollback()
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=5)


async def test_old_checkout_cannot_extend_changed_target(postgres_database, monkeypatch):
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        sub = await db.get(Subscription, 1)
        sub.device_limit = 5
        await db.commit()
        await service.process_cispay_subscription_charge(db, payment())
        assert (await db.get(User, 1)).balance_kopeks == 10000
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=5)
        await service.process_cispay_subscription_charge(db, payment())
        assert (await db.get(User, 1)).balance_kopeks == 10000


async def test_cancel_timeout_keeps_pending_and_retry_confirms(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.models import CisPaySubscription
    from app.services import cispay_recurrent_service as service
    from app.services.cispay_service import CisPayAPIError, cispay_service

    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    remote = dict(
        id=REMOTE,
        status='CANCELLED',
        customer_id='user-1',
        amount=10000,
        currency='RUB',
        payment_method='SBP',
        interval_days=30,
        subscription_purpose='Test',
    )
    cancel = AsyncMock(side_effect=[CisPayAPIError(), remote])
    monkeypatch.setattr(cispay_service, 'cancel_subscription', cancel)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        state = await service.cancel_cispay_agreement(db, user_id=1, subscription_id=1, reason='customer')
        assert state['status'] == 'CANCEL_PENDING'
        state = await service.cancel_cispay_agreement(db, user_id=1, subscription_id=1, reason='customer')
        assert state['status'] == 'CANCELLED'
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=5)
        assert (await db.get(CisPaySubscription, 1)).status == 'CANCELLED'


async def test_lifecycle_updates_state_without_grant(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.models import CisPaySubscription
    from app.services import cispay_recurrent_service as service
    from app.services.cispay_service import cispay_service

    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    remote = dict(
        id=REMOTE,
        status='CANCELLED',
        customer_id='user-1',
        amount=10000,
        currency='RUB',
        payment_method='SBP',
        interval_days=30,
        subscription_purpose='Test',
        ended_reason='unbound_in_bank',
    )
    monkeypatch.setattr(cispay_service, 'get_subscription', AsyncMock(return_value=remote))
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        await service.process_cispay_subscription_event(
            db,
            dict(id=REMOTE, store_id='shop', object='subscription', event='subscription.cancelled', status='CANCELLED'),
        )
        assert (await db.get(CisPaySubscription, 1)).status == 'CANCELLED'
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=5)


async def test_reconcile_lost_charge_from_terminal_agreement(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.models import CisPaySubscription
    from app.services import cispay_recurrent_service as service
    from app.services.cispay_service import cispay_service

    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'test-key')
    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    remote = dict(
        id=REMOTE,
        status='CANCELLED',
        customer_id='user-1',
        amount=10000,
        currency='RUB',
        payment_method='SBP',
        interval_days=30,
        subscription_purpose='Test',
        charges=[{'id': CHARGE, 'status': 'PAID', 'amount': 10000, 'paid_at': NOW.isoformat()}],
    )
    monkeypatch.setattr(cispay_service, 'get_subscription', AsyncMock(return_value=remote))
    monkeypatch.setattr(
        cispay_service,
        'list_transactions',
        AsyncMock(
            return_value={
                'items': [
                    {
                        'id': CHARGE,
                        'status': 'PAID',
                        'amount': 10000,
                        'payment_method': 'SBP',
                        'is_sandbox': False,
                        'paid_at': NOW.isoformat(),
                        'customer_id': 'user-1',
                    }
                ],
                'has_more': False,
            }
        ),
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        agreement = await db.get(CisPaySubscription, 1)
        agreement.status = 'CANCELLED'
        await db.commit()
        await service.reconcile_cispay_agreements(db)
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=35)
        assert (await db.get(CisPaySubscription, 1)).status == 'CANCELLED'


async def test_trial_converts_only_paid_once_preserving_identity(postgres_database, monkeypatch):
    from app.database.models import CisPaySubscription
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        target = await db.get(Subscription, 1)
        target.is_trial = True
        target.device_limit = 1
        agreement = await db.get(CisPaySubscription, 1)
        agreement.snapshot = {**agreement.snapshot, 'trial_source': {'tariff_id': 1, 'device_limit': 1}}
        await db.commit()
        await service.process_cispay_subscription_charge(db, payment())
        await service.process_cispay_subscription_charge(db, payment())
        await db.refresh(target)
        assert target.is_trial is False
        assert target.device_limit == 2
        assert target.status == 'active'
        assert (await db.get(User, 1)).balance_kopeks == 0
        assert (await db.get(User, 1)).has_had_paid_subscription is True


async def test_new_target_delivery_creates_panel_identity_and_finishes_once(postgres_database, monkeypatch):
    from app.database.models import CisPaySubscriptionCharge
    from app.services import cispay_recurrent_service as service
    from app.services.subscription_service import SubscriptionService

    create = AsyncMock(return_value=object())
    update = AsyncMock(return_value=object())
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', create)
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', update)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        await service.process_cispay_subscription_charge(db, payment())
        charge = await db.scalar(select(CisPaySubscriptionCharge))
        assert charge.delivery_completed_at is not None
        await service.deliver_cispay_charge(db, charge.id)
        assert create.await_count == 1
        assert update.await_count == 0


async def test_manual_change_queues_cancel_in_same_transaction(postgres_database):
    from app.database.models import CisPaySubscription
    from app.services.cispay_recurrent_guard import stage_cispay_cancellation

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        await stage_cispay_cancellation(db, 1, reason='manual_change')
        assert (await db.get(CisPaySubscription, 1)).status == 'CANCEL_PENDING'
        await db.rollback()
        assert (await db.get(CisPaySubscription, 1)).status == 'ACTIVE'


async def test_credentials_cannot_be_discarded_with_live_agreement(postgres_database):
    from app.services.cispay_recurrent_guard import guard_cispay_credentials

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        with pytest.raises(ValueError, match='CisPay'):
            await guard_cispay_credentials(db, 'CISPAY_SHOP_ID', 'other-shop')
        with pytest.raises(ValueError, match='CisPay'):
            await guard_cispay_credentials(db, 'CISPAY_API_KEY', '')
        await guard_cispay_credentials(db, 'CISPAY_RECURRENT_ENABLED', False)
        await guard_cispay_credentials(db, 'CISPAY_SHOP_ID', 'shop')


async def test_signed_http_webhook_delivers_once_with_feature_off(postgres_database, monkeypatch):
    import hashlib
    import hmac
    import json
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from app.config import settings
    from app.services import cispay_recurrent_service as service
    from app.services.payment.cispay import CisPayPaymentMixin
    from app.webserver import payments

    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'synthetic-secret')
    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)

        async def process(_service, payload, _method):
            return await CisPayPaymentMixin().process_cispay_callback(db, payload)

        monkeypatch.setattr(payments, '_process_payment_service_callback', process)
        app = FastAPI()
        app.include_router(payments.create_payment_router(SimpleNamespace(), SimpleNamespace()))
        body = json.dumps(payment()).encode()
        signature = hmac.new(b'synthetic-secret', body, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            denied = await client.post(settings.CISPAY_WEBHOOK_PATH, content=body, headers={'X-Signature': 'invalid'})
            assert denied.status_code == 400
            for _ in range(2):
                response = await client.post(
                    settings.CISPAY_WEBHOOK_PATH, content=body, headers={'X-Signature': signature}
                )
                assert response.status_code == 200, response.text
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=35)


async def test_other_provider_and_manual_extension_blocked_until_cancelled(postgres_database, monkeypatch):
    from app.config import settings
    from app.database.crud.subscription import extend_subscription
    from app.database.models import CisPaySubscription
    from app.services.cispay_recurrent_guard import require_no_cispay

    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        with pytest.raises(ValueError, match='CisPay'):
            await require_no_cispay(db, user_id=1, subscription_id=1)
        with pytest.raises(ValueError, match='CisPay'):
            await extend_subscription(db, await db.get(Subscription, 1), 10)
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=5)
        agreement = await db.get(CisPaySubscription, 1)
        agreement.status = 'CANCELLED'
        await db.commit()
        await require_no_cispay(db, user_id=1, subscription_id=1)


async def test_manual_expiry_change_late_charge_goes_to_balance(postgres_database, monkeypatch):
    from app.database.models import CisPaySubscription
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        agreement = await db.get(CisPaySubscription, 1)
        agreement.expected_end_date = NOW + timedelta(days=5)
        target = await db.get(Subscription, 1)
        target.end_date += timedelta(days=10)
        await db.commit()
        await service.process_cispay_subscription_charge(db, payment())
        assert target.end_date == NOW + timedelta(days=15)
        assert (await db.get(User, 1)).balance_kopeks == 10000
        assert agreement.status == 'CANCEL_PENDING'


async def test_admin_bulk_device_change_is_rejected_before_mutation(postgres_database):
    from app.cabinet.routes.admin_bulk_actions import _do_set_devices
    from app.cabinet.schemas.bulk_actions import BulkActionParams

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        user = await db.get(User, 1)
        target = await db.get(Subscription, 1)
        result = await _do_set_devices(db, user, BulkActionParams(device_limit=5), False, sub_override=target)
        assert result.success is False
        assert 'CisPay' in result.message
        await db.refresh(target)
        assert target.device_limit == 2


async def test_refund_before_paid_does_not_grant_access(postgres_database, monkeypatch):
    from app.database.models import CisPaySubscriptionCharge
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        await service.process_cispay_subscription_charge(db, payment(status='REFUNDED'))
        await service.process_cispay_subscription_charge(db, payment())
        assert (await db.get(Subscription, 1)).end_date == NOW + timedelta(days=5)
        charge = await db.scalar(select(CisPaySubscriptionCharge))
        assert charge.status == 'REFUND_REVIEW'
        assert (await db.get(User, 1)).balance_kopeks == 0


@pytest.mark.parametrize('delete_user', [False, True])
async def test_deleted_target_keeps_late_money_accounted(postgres_database, monkeypatch, delete_user):
    from sqlalchemy import delete

    from app.database.models import CisPaySubscriptionCharge
    from app.services import cispay_recurrent_service as service

    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        await db.execute(delete(Subscription).where(Subscription.id == 1))
        if delete_user:
            await db.execute(delete(User).where(User.id == 1))
        await db.commit()
        await service.process_cispay_subscription_charge(db, payment())
        charge = await db.scalar(select(CisPaySubscriptionCharge))
        assert charge.status == ('REVIEW' if delete_user else 'BALANCE')
        if not delete_user:
            assert (await db.get(User, 1)).balance_kopeks == 10000
        assert await db.get(Subscription, 1) is None
