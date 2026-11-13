"""Cross-provider recurring guards survive feature switches and cancellation."""

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, PayHotAgreement, Subscription, User
from app.services.payhot_recurrent_guard import (
    guard_payhot_credentials,
    has_conflicting_payhot,
    require_no_payhot,
    stage_payhot_cancellation,
)
from tests.database.test_payhot_recurrent_journal_postgres import END, seed
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('multi', [False, True])
async def test_selected_subscription_scope_and_disabled_feature_still_guarded(postgres_database, monkeypatch, multi):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', False)
    monkeypatch.setattr(settings, 'CISPAY_RECURRENT_ENABLED', False)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        db.add(Subscription(id=2, remnawave_short_id='second', user_id=1, end_date=END, status='active'))
        await db.commit()
        assert await has_conflicting_payhot(db, user_id=1, subscription_id=1)
        assert await has_conflicting_payhot(db, user_id=1, subscription_id=2) is not multi
        from app.services.cispay_recurrent_guard import has_conflicting_cispay, require_no_cispay

        assert await has_conflicting_cispay(db, user_id=1, subscription_id=1)
        with pytest.raises(ValueError, match='HOT Pay'):
            await require_no_cispay(db, user_id=1, subscription_id=1)
        await db.rollback()


async def test_cancellation_is_local_transactional_and_remains_conflicting(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        aid = await seed(db)
        await stage_payhot_cancellation(db, 1, reason='price_changed')
        await db.rollback()
        assert (await other.get(PayHotAgreement, aid)).status == 'ACTIVE'
        await other.rollback()
        await stage_payhot_cancellation(db, 1, reason='price_changed')
        await db.commit()
        agreement = await other.get(PayHotAgreement, aid, populate_existing=True)
        assert agreement.status == 'CANCEL_PENDING' and agreement.cancel_requested_at
        assert await has_conflicting_payhot(other, user_id=1, subscription_id=1)
        agreement.status = 'CANCELLED'
        await other.commit()
        assert not await has_conflicting_payhot(db, user_id=1, subscription_id=1)


@pytest.mark.parametrize(
    'key,value',
    [('PAYHOT_API_KEY', 'new-key'), ('PAYHOT_API_URL', 'https://gapi.pay.hot/api/v2'), ('PAYHOT_WEBHOOK_SECRET', '')],
)
async def test_live_agreement_keeps_credentials_for_cancel_and_recovery(postgres_database, monkeypatch, key, value):
    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'current-key')
    monkeypatch.setattr(settings, 'PAYHOT_API_URL', 'https://app.pay.hot/api/v2')
    monkeypatch.setattr(settings, 'PAYHOT_WEBHOOK_SECRET', 'current-secret')
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        with pytest.raises(ValueError, match='HOT Pay'):
            await guard_payhot_credentials(db, key, value)
        await guard_payhot_credentials(db, key, getattr(settings, key))


async def test_other_user_agreement_never_blocks_owner(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        db.add(User(id=2, email='other@example.com'))
        await db.flush()
        db.add(Subscription(id=2, remnawave_short_id='second', user_id=2, end_date=END, status='active'))
        await db.commit()
        assert not await has_conflicting_payhot(db, user_id=2, subscription_id=2)
        await require_no_payhot(db, user_id=2, subscription_id=2)


async def test_daily_billing_skips_with_live_hotpay_even_when_feature_disabled(postgres_database, monkeypatch):
    from sqlalchemy.orm import selectinload

    from app.database.models import Tariff
    from app.services.daily_subscription_service import DailySubscriptionService

    monkeypatch.setattr(settings, 'PAYHOT_ENABLED', False)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        db.add(Tariff(id=1, name='Daily example', is_daily=True, daily_price_kopeks=100, period_prices={}))
        await db.flush()
        sub = await db.get(Subscription, 1)
        sub.tariff_id = 1
        user = await db.get(User, 1)
        user.balance_kopeks = 10000
        await db.commit()
        sub = await db.scalar(
            select(Subscription)
            .where(Subscription.id == 1)
            .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
            .execution_options(populate_existing=True)
        )
        result = await DailySubscriptionService()._process_single_charge(db, sub)
        assert result == 'skipped'
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10000


async def test_ordinary_manual_renewal_is_blocked_before_entitlement_change(postgres_database):
    from app.database.crud.subscription import extend_subscription

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        sub = await db.get(Subscription, 1)
        with pytest.raises(ValueError, match='HOT Pay'):
            await extend_subscription(db, sub, 30, commit=False)
        await db.rollback()
        assert (await db.get(Subscription, 1)).end_date == END


async def test_account_block_stages_cancellation_without_moving_identity(postgres_database):
    from app.database.crud.user import update_user

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        user = await db.get(User, 1)
        await update_user(db, user, status='blocked')
        agreement = await db.get(PayHotAgreement, aid, populate_existing=True)
        assert agreement.status == 'CANCEL_PENDING' and agreement.owner_id == 1
        assert agreement.cancel_requested_at


async def test_merge_cancels_but_never_transfers_bank_consent(postgres_database):
    from app.services.account_merge_service import execute_merge

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        db.add(User(id=2, email='target@example.com'))
        await db.commit()
        await execute_merge(db, primary_user_id=2, secondary_user_id=1)
        await db.commit()
        agreement = await db.get(PayHotAgreement, aid, populate_existing=True)
        assert agreement.status == 'CANCEL_PENDING'
        assert agreement.owner_id == agreement.user_id == 1


async def test_cispay_other_recurring_detects_hotpay(postgres_database):
    from app.services.cispay_recurrent_checkout import _other_recurring

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        await seed(db)
        assert await _other_recurring(db, 1)


async def seed_first_invoice(db, aid):
    from app.database.models import PayHotPayment

    invoice = PayHotPayment(user_id=1, order_id='first-example', amount_kopeks=10000, currency='RUB', status='creating')
    ordinary = PayHotPayment(
        user_id=1, order_id='ordinary-example', amount_kopeks=10000, currency='RUB', status='creating'
    )
    db.add_all([invoice, ordinary])
    await db.flush()
    (await db.get(PayHotAgreement, aid)).first_payment_id = invoice.id
    await db.commit()
    return invoice.id, ordinary.id


async def test_physical_owner_cleanup_retains_recurring_first_invoice(postgres_database, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.database.models import PayHotPayment
    from app.services.blocked_users_service import BlockedUsersService

    for module in ('platega', 'lava'):
        monkeypatch.setattr(
            f'app.services.payment.{module}.cancel_{module}_recurring_for_subscription_safe', AsyncMock()
        )
    monkeypatch.setattr(
        'app.services.cashera_recurring_cancel.cancel_cashera_recurring_for_subscription_safe', AsyncMock()
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        aid = await seed(db)
        iid, ordinary_id = await seed_first_invoice(db, aid)
        assert await BlockedUsersService(SimpleNamespace()).delete_user_from_db(db, 1)
        invoice = await other.get(PayHotPayment, iid)
        agreement = await other.get(PayHotAgreement, aid)
        assert invoice is not None and invoice.user_id is None
        assert agreement.first_payment_id == iid and agreement.owner_id == 1 and agreement.user_id is None
        assert agreement.status == 'CANCEL_PENDING'
        assert await other.get(PayHotPayment, ordinary_id) is None


async def test_merge_never_transfers_mandate_first_invoice(postgres_database):
    from app.database.models import PayHotPayment
    from app.services.account_merge_service import execute_merge

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        aid = await seed(db)
        iid, ordinary_id = await seed_first_invoice(db, aid)
        db.add(User(id=2, email='target@example.com'))
        await db.commit()
        await execute_merge(db, primary_user_id=2, secondary_user_id=1)
        await db.commit()
        assert (await db.get(PayHotPayment, iid, populate_existing=True)).user_id == 1
        assert (await db.get(PayHotPayment, ordinary_id, populate_existing=True)).user_id == 2
