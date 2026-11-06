"""Cross-flow billing regressions: real database, no provider/network payments."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Base, CisPaySubscription, Subscription, Tariff, User
from tests.database.test_cispay_recurrent_charges_postgres import seed
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def test_daily_worker_never_debits_while_bank_agreement_is_live(postgres_database, monkeypatch):
    from app.services.daily_subscription_service import DailySubscriptionService

    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        user = await db.get(User, 1)
        user.balance_kopeks = 50000
        tariff = await db.get(Tariff, 1)
        tariff.is_daily = True
        tariff.daily_price_kopeks = 100
        sub = await db.get(Subscription, 1)
        sub.end_date = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        sub = await db.scalar(
            select(Subscription)
            .where(Subscription.id == 1)
            .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        )
        result = await DailySubscriptionService()._process_single_charge(db, sub)
        await db.refresh(user)
        assert user.balance_kopeks == 50000
        assert result == 'skipped'


async def test_tariff_switch_preflight_refuses_live_agreement(postgres_database):
    from app.services.traffic_purchase_service import prepare_user_tariff_switch

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        with pytest.raises(ValueError, match='CisPay'):
            await prepare_user_tariff_switch(db, await db.get(Subscription, 1))


async def test_disabled_subscription_cannot_open_bank_checkout(postgres_database, monkeypatch):
    from app.services import cispay_recurrent_checkout as checkout

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(
        checkout,
        'get_cispay_recurrent_policy',
        AsyncMock(
            return_value={'enabled': True, 'checkout_policy': {'min_amount_kopeks': 1, 'max_amount_kopeks': 100000}}
        ),
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        agreement = await db.get(CisPaySubscription, 1)
        agreement.status = 'CANCELLED'
        target = await db.get(Subscription, 1)
        target.status = 'disabled'
        target.autopay_enabled = False
        await db.commit()
        with pytest.raises(ValueError, match='subscription_disabled'):
            await checkout._terms(
                db,
                user=await db.get(User, 1),
                tariff_id=1,
                period_days=30,
                device_limit=2,
                subscription_id=1,
                channel='cabinet',
            )


async def test_saved_daily_cart_never_reaches_balance_debit_with_live_bank(postgres_database, monkeypatch):
    from app.database.crud import user as user_crud
    from app.services.subscription_auto_purchase_service import _auto_purchase_daily_tariff

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    debit = AsyncMock(return_value=False)
    monkeypatch.setattr(user_crud, 'subtract_user_balance', debit)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        user = await db.get(User, 1)
        user.balance_kopeks = 50000
        tariff = await db.get(Tariff, 1)
        tariff.is_daily = True
        tariff.daily_price_kopeks = 100
        target = await db.get(Subscription, 1)
        target.end_date = datetime.now(UTC) - timedelta(days=1)
        target.status = 'expired'
        await db.commit()
        assert await _auto_purchase_daily_tariff(db, user, {'tariff_id': 1, 'subscription_id': 1}) is False
        debit.assert_not_awaited()


async def test_cabinet_purchase_target_blocks_bank_before_charging(postgres_database):
    from fastapi import HTTPException

    from app.cabinet.routes.subscription_modules.purchase import _resolve_tariff_purchase_target
    from app.cabinet.schemas.subscription import TariffPurchaseRequest

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        with pytest.raises(HTTPException) as error:
            await _resolve_tariff_purchase_target(
                db,
                await db.get(User, 1),
                await db.get(Tariff, 1),
                TariffPurchaseRequest(tariff_id=1, period_days=30, subscription_id=1),
            )
        assert error.value.status_code == 409
        assert 'CisPay' in str(error.value.detail)


async def test_telegram_switch_resolver_refuses_live_bank(postgres_database, monkeypatch):
    from types import SimpleNamespace

    from app.handlers.subscription.tariff_purchase import _resolve_switch_subscription

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        callback = SimpleNamespace(data='daily_tariff_switch_confirm:2', answer=AsyncMock())
        result = await _resolve_switch_subscription(callback, await db.get(User, 1), db)
        assert result == (None, None)
        assert 'CisPay' in callback.answer.await_args.args[0]


async def test_daily_resume_preflight_refuses_live_bank(postgres_database):
    from app.services.traffic_purchase_service import lock_daily_accounting_subscription

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        with pytest.raises(ValueError, match='CisPay'):
            await lock_daily_accounting_subscription(db, await db.get(Subscription, 1), 1)


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('source', ['new', 'trial', 'paid'])
@pytest.mark.parametrize('days', [1, 6, 7, 10, 360, 361, 365, 366, 10000])
async def test_exact_period_matrix(postgres_database, monkeypatch, multi, source, days):
    from sqlalchemy import delete

    from app.services import cispay_recurrent_checkout as checkout

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(
        checkout,
        'get_cispay_recurrent_policy',
        AsyncMock(
            return_value={'enabled': True, 'checkout_policy': {'min_amount_kopeks': 1, 'max_amount_kopeks': 100000}}
        ),
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        (await db.get(CisPaySubscription, 1)).status = 'CANCELLED'
        tariff = await db.get(Tariff, 1)
        tariff.period_prices = {str(days): 10000}
        target = await db.get(Subscription, 1)
        target.autopay_enabled = False
        target.is_trial = source == 'trial'
        target_id = 1
        if source == 'new':
            await db.execute(delete(Subscription).where(Subscription.id == 1))
            target_id = None
        await db.commit()
        args = dict(
            user=await db.get(User, 1),
            tariff_id=1,
            period_days=days,
            device_limit=2,
            subscription_id=target_id,
            channel='cabinet',
        )
        if 7 <= days <= 360:
            quote = await checkout._terms(db, **args)
            assert quote['period_days'] == days
            assert quote['amount_kopeks'] == 10000
            assert quote['source_subscription_id'] == target_id
        else:
            with pytest.raises(ValueError, match='7 до 360'):
                await checkout._terms(db, **args)


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize(
    'bank_state', ['CREATING', 'CREATION_UNKNOWN', 'PENDING', 'ACTIVE', 'PAUSED', 'CANCEL_PENDING', 'CANCELLED']
)
async def test_balance_autopay_respects_bank_and_multi_target(postgres_database, monkeypatch, multi, bank_state):
    from app.services.autopay_guard import lock_autopay_target

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    expiry = datetime.now(UTC) - timedelta(minutes=1)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        (await db.get(CisPaySubscription, 1)).status = bank_state
        first = await db.get(Subscription, 1)
        first.autopay_enabled = True
        first.end_date = expiry
        db.add(Tariff(id=2, name='Other', period_prices={'30': 10000}, device_limit=2))
        await db.flush()
        db.add(
            Subscription(
                id=2,
                remnawave_short_id='masked-2',
                user_id=1,
                tariff_id=2,
                is_trial=False,
                status='active',
                autopay_enabled=True,
                end_date=expiry,
                start_date=expiry - timedelta(days=30),
            )
        )
        await db.commit()
        result = await lock_autopay_target(db, 1, 1, expected_end_date=expiry)
        assert (result is not None) == (bank_state == 'CANCELLED')
        await db.commit()
        other = await lock_autopay_target(db, 1, 2, expected_end_date=expiry)
        assert (other is not None) == (multi or bank_state == 'CANCELLED')


@pytest.mark.parametrize('multi', [False, True])
async def test_second_bank_target_only_allowed_in_multi(postgres_database, monkeypatch, multi):
    from app.services import cispay_recurrent_checkout as checkout

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(
        checkout,
        'get_cispay_recurrent_policy',
        AsyncMock(
            return_value={'enabled': True, 'checkout_policy': {'min_amount_kopeks': 1, 'max_amount_kopeks': 100000}}
        ),
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        db.add(Tariff(id=2, name='Other', device_limit=2, period_prices={'30': 10000}, is_active=True))
        await db.flush()
        db.add(
            Subscription(
                id=2,
                remnawave_short_id='masked-2',
                user_id=1,
                tariff_id=2,
                status='expired',
                is_trial=False,
                device_limit=2,
                autopay_enabled=False,
                end_date=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await db.commit()
        args = dict(
            user=await db.get(User, 1),
            tariff_id=2,
            period_days=30,
            device_limit=2,
            subscription_id=2,
            channel='cabinet',
        )
        if multi:
            assert (await checkout._terms(db, **args))['source_subscription_id'] == 2
        else:
            with pytest.raises(ValueError, match='recurrent_already_exists'):
                await checkout._terms(db, **args)


@pytest.mark.parametrize('trial', [False, True])
@pytest.mark.parametrize('changed', ['traffic_limit_gb', 'allowed_squads'])
@pytest.mark.parametrize('stage', ['worker', 'charge'])
async def test_same_price_changed_tariff_terms_invalidate_consent(
    postgres_database, monkeypatch, trial, changed, stage
):
    from app.services import cispay_recurrent_checkout as checkout, cispay_recurrent_service as service
    from tests.database.test_cispay_recurrent_charges_postgres import payment

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(
        checkout,
        'get_cispay_recurrent_policy',
        AsyncMock(
            return_value={'enabled': True, 'checkout_policy': {'min_amount_kopeks': 1, 'max_amount_kopeks': 100000}}
        ),
    )
    monkeypatch.setattr(service, 'deliver_cispay_charge', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        agreement = await db.get(CisPaySubscription, 1)
        agreement.status = 'CANCELLED'
        target = await db.get(Subscription, 1)
        target.is_trial = trial
        target.autopay_enabled = False
        target.traffic_limit_gb = 100
        tariff = await db.get(Tariff, 1)
        tariff.traffic_limit_gb = 100
        tariff.allowed_squads = ['masked-1']
        await db.commit()
        terms = await checkout._terms(
            db,
            user=await db.get(User, 1),
            tariff_id=1,
            period_days=30,
            device_limit=2,
            subscription_id=1,
            channel='cabinet',
        )
        agreement.snapshot = terms
        agreement.status = 'ACTIVE'
        setattr(tariff, changed, 10 if changed == 'traffic_limit_gb' else ['masked-2'])
        await db.commit()
        expiry = target.end_date
        if stage == 'worker':
            await service._queue_changed_agreement(db, agreement.id)
        else:
            await service.process_cispay_subscription_charge(db, payment())
            await db.refresh(target)
            assert target.end_date == expiry
            assert target.is_trial == trial
            assert (await db.get(User, 1)).balance_kopeks == 10000
        await db.refresh(agreement)
        assert agreement.status == 'CANCEL_PENDING'


@pytest.mark.parametrize('multi', [False, True])
async def test_old_target_cannot_bypass_current_subscription(postgres_database, monkeypatch, multi):
    from app.services import cispay_recurrent_checkout as checkout

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(
        checkout,
        'get_cispay_recurrent_policy',
        AsyncMock(
            return_value={'enabled': True, 'checkout_policy': {'min_amount_kopeks': 1, 'max_amount_kopeks': 100000}}
        ),
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        (await db.get(CisPaySubscription, 1)).status = 'CANCELLED'
        db.add(Tariff(id=2, name='Other', device_limit=2, period_prices={'30': 10000}, is_active=True))
        await db.flush()
        db.add(
            Subscription(
                id=2,
                remnawave_short_id='masked-2',
                user_id=1,
                tariff_id=2,
                status='expired',
                is_trial=False,
                device_limit=2,
                autopay_enabled=False,
                end_date=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await db.commit()
        args = dict(
            user=await db.get(User, 1),
            tariff_id=2,
            period_days=30,
            device_limit=2,
            subscription_id=2,
            channel='cabinet',
        )
        if multi:
            assert (await checkout._terms(db, **args))['source_subscription_id'] == 2
        else:
            with pytest.raises(ValueError, match='select_existing_subscription'):
                await checkout._terms(db, **args)
