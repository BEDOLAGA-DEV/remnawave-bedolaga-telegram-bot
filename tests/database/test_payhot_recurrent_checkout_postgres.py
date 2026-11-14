"""Shared authenticated HOT Pay checkout against real PostgreSQL."""

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import Base, PayHotAgreement, PayHotPayment, Subscription, Tariff, User
from app.services import payhot_recurrent_checkout as checkout
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def setup_policy(monkeypatch):
    monkeypatch.setitem(settings.__dict__, 'PAYHOT_RECURRENT_ENABLED', True)
    monkeypatch.setattr(settings, 'PAYHOT_API_KEY', 'phk_v2_synthetic')
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    from app.services import payhot_recurrent_policy, tariff_purchase_quote

    monkeypatch.setattr(
        payhot_recurrent_policy,
        'get_payhot_checkout_policy',
        AsyncMock(
            return_value={
                'min_amount_kopeks': 1,
                'max_amount_kopeks': 2147483647,
                'options': [{'id': 'card'}, {'id': 'sbp'}],
            }
        ),
    )
    saved = {}

    async def save(user_id, channel, terms):
        q = uuid4()
        saved[(user_id, channel, str(q))] = terms
        return q, datetime.now(UTC) + timedelta(minutes=5)

    async def load(user_id, channel, q):
        return saved[(user_id, channel, str(q))]

    monkeypatch.setattr(tariff_purchase_quote, 'save_purchase_quote', save)
    monkeypatch.setattr(tariff_purchase_quote, 'load_purchase_quote', load)


async def seed(db, *, trial=False, daily=False):
    user = User(id=1, email='payer@example.com')
    tariff = Tariff(
        id=1,
        name='Example',
        is_active=True,
        device_limit=2,
        max_device_limit=10,
        device_price_kopeks=3000,
        traffic_limit_gb=100,
        period_prices={'6': 6000, '30': 30000, '10000': 1000000},
        is_daily=daily,
        daily_price_kopeks=1000,
    )
    db.add_all([user, tariff])
    await db.flush()
    sub = Subscription(
        user_id=1,
        tariff_id=None if trial else 1,
        is_trial=trial,
        status='active',
        end_date=datetime.now(UTC) + timedelta(days=5),
        device_limit=5,
        autopay_enabled=False,
        daily_devices_billing_version=1,
        connected_squads=[],
    )
    db.add(sub)
    await db.commit()
    return user, tariff, sub


@pytest.mark.parametrize('channel', ['telegram', 'cabinet'])
async def test_existing_inline_selected_target_retains_devices_and_exact_period(postgres_database, channel):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, _ = await seed(db)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel=channel
        )
        assert q['device_limit'] == 5 and q['period_days'] == 6 and q['subscription_id'] == 1
        assert not {'api_key_fingerprint', 'source_terms', 'tariff_terms', 'trial_source'} & q.keys()
        with pytest.raises(ValueError, match='device_selection'):
            await checkout.quote_payhot_purchase(
                db, user=user, tariff_id=1, period_days=6, subscription_id=1, device_limit=3, channel=channel
            )


async def test_purchase_reserves_first_payment_before_network_and_replays(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=2) as (db, observer):
        user, _, _ = await seed(db)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=30, subscription_id=1, channel='cabinet'
        )

        async def create(session, agreement_id):
            agreement = await observer.get(PayHotAgreement, agreement_id, populate_existing=True)
            assert agreement is not None
            payment = await observer.get(PayHotPayment, agreement.first_payment_id)
            assert payment.metadata_json['purpose'] == 'payhot_recurring'
            assert payment.request_payload['recurring_enrollment'] == {}
            assert payment.request_payload['customer']['id'] == 'user-1'
            await observer.rollback()
            return {'id': agreement_id, 'status': 'PENDING'}

        monkeypatch.setitem(
            sys.modules, 'app.services.payhot_recurrent_service', SimpleNamespace(create_payhot_agreement=create)
        )
        cid = str(uuid4())
        first = await checkout.purchase_payhot_quote(
            db, user=user, quote_id=q['quote_id'], checkout_id=cid, channel='cabinet'
        )
        replay = await checkout.purchase_payhot_quote(
            db, user=user, quote_id=q['quote_id'], checkout_id=cid, channel='cabinet'
        )
        assert first == replay
        assert await db.scalar(select(func.count(PayHotAgreement.id))) == 1
        assert await db.scalar(select(func.count(PayHotPayment.id))) == 1


async def test_changed_entitlements_reject_same_price(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, tariff, _ = await seed(db)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=30, subscription_id=1, channel='cabinet'
        )
        tariff.external_squad_uuid = 'masked-external'
        await db.commit()
        with pytest.raises(ValueError, match='quote_changed'):
            await checkout.purchase_payhot_quote(
                db, user=user, quote_id=q['quote_id'], checkout_id=str(uuid4()), channel='cabinet'
            )
        assert await db.scalar(select(func.count(PayHotPayment.id))) == 0


async def test_foreign_target_and_balance_autopay_rejected(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, sub = await seed(db)
        with pytest.raises(ValueError, match='subscription_not_found'):
            await checkout.quote_payhot_purchase(
                db, user=user, tariff_id=1, period_days=30, subscription_id=999, channel='telegram'
            )
        sub.autopay_enabled = True
        await db.commit()
        with pytest.raises(ValueError, match='disable_balance_autopay'):
            await checkout.quote_payhot_purchase(
                db, user=user, tariff_id=1, period_days=30, subscription_id=1, channel='telegram'
            )


async def test_daily_uses_existing_device_pricing(postgres_database):
    from app.services.pricing_engine import PricingEngine

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, tariff, sub = await seed(db, daily=True)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=1, subscription_id=1, channel='cabinet'
        )
        assert q['amount_kopeks'] == PricingEngine.daily_subscription_price(sub, tariff, user)
        assert q['amount_kopeks'] > tariff.daily_price_kopeks


async def test_trial_conversion_selects_new_devices_and_long_custom_period(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, sub = await seed(db, trial=True)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=10000, subscription_id=1, device_limit=7, channel='cabinet'
        )
        assert q['device_limit'] == 7
        from app.services.tariff_purchase_quote import load_purchase_quote

        q = await load_purchase_quote(user.id, 'payhot:cabinet', q['quote_id'])
        assert q['trial_source'] == {'tariff_id': None, 'device_limit': 5}
        assert await checkout.current_payhot_terms_match(db, sub, q, first=True)
        sub.device_limit = 6
        await db.commit()
        assert not await checkout.current_payhot_terms_match(db, sub, q, first=True)


async def test_new_target_is_inert_before_external_checkout(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        user, _, sub = await seed(db)
        sub.status = 'expired'
        await db.commit()
        q = await checkout.quote_payhot_purchase(db, user=user, tariff_id=1, period_days=6, channel='telegram')

        async def create(session, agreement_id):
            agreement = await observer.get(PayHotAgreement, agreement_id)
            target = await observer.get(Subscription, agreement.subscription_id)
            assert target.status == 'expired' and target.end_date <= datetime.now(UTC)
            assert not target.autopay_enabled
            assert await checkout.current_payhot_terms_match(observer, target, agreement.consent_snapshot, first=True)
            return {'id': agreement_id}

        monkeypatch.setitem(
            sys.modules, 'app.services.payhot_recurrent_service', SimpleNamespace(create_payhot_agreement=create)
        )
        await checkout.purchase_payhot_quote(
            db, user=user, quote_id=q['quote_id'], checkout_id=str(uuid4()), channel='telegram'
        )


async def test_classic_paid_uses_canonical_renewal_and_preserves_devices(postgres_database, monkeypatch):
    from app.services.recurrent_amount import resolve_true_renewal_amount

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, sub = await seed(db)
        sub.tariff_id = None
        await db.commit()
        monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: False)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=None, period_days=30, subscription_id=1, channel='cabinet'
        )
        assert q['amount_kopeks'] == await resolve_true_renewal_amount(db, sub, 30)
        assert q['device_limit'] == 5 and q['tariff_id'] is None
        from app.services.tariff_purchase_quote import load_purchase_quote

        snapshot = await load_purchase_quote(user.id, 'payhot:cabinet', q['quote_id'])
        assert await checkout.current_payhot_terms_match(db, sub, snapshot)


@pytest.mark.parametrize('field,value', [('device_limit', 6), ('end_date', datetime(2040, 1, 1, tzinfo=UTC))])
async def test_quote_changes_before_purchase_fail_without_invoice(postgres_database, field, value):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, sub = await seed(db)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel='cabinet'
        )
        setattr(sub, field, value)
        await db.commit()
        with pytest.raises(ValueError):
            await checkout.purchase_payhot_quote(
                db, user=user, quote_id=q['quote_id'], checkout_id=str(uuid4()), channel='cabinet'
            )
        assert await db.scalar(select(func.count(PayHotPayment.id))) == 0


async def test_state_is_owned_and_survives_disabled_provider(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, _ = await seed(db)
        db.add(
            PayHotAgreement(
                user_id=1,
                owner_id=1,
                subscription_id=1,
                checkout_id='example-checkout',
                status='CANCEL_PENDING',
                consent_snapshot={'amount_kopeks': 10000, 'period_days': 6, 'payment_method': 'card'},
            )
        )
        await db.commit()
        assert (await checkout.get_payhot_agreement_state(db, user_id=1, subscription_id=1))[
            'status'
        ] == 'CANCEL_PENDING'
        with pytest.raises(ValueError, match='subscription_not_found'):
            await checkout.get_payhot_agreement_state(db, user_id=2, subscription_id=1)


@pytest.mark.parametrize('same_checkout', [True, False])
async def test_concurrent_purchase_never_creates_duplicate_agreement(postgres_database, monkeypatch, same_checkout):
    import asyncio

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=3) as (db, one, two):
        user, _, _ = await seed(db)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel='cabinet'
        )
        await db.commit()

        async def create(session, agreement_id):
            return {'id': agreement_id}

        monkeypatch.setitem(
            sys.modules, 'app.services.payhot_recurrent_service', SimpleNamespace(create_payhot_agreement=create)
        )
        cid = str(uuid4())

        async def purchase(session, identifier):
            return await checkout.purchase_payhot_quote(
                session, user=SimpleNamespace(id=1), quote_id=q['quote_id'], checkout_id=identifier, channel='cabinet'
            )

        results = await asyncio.gather(
            purchase(one, cid), purchase(two, cid if same_checkout else str(uuid4())), return_exceptions=True
        )
        if same_checkout:
            assert results[0] == results[1] and isinstance(results[0], dict)
        else:
            assert sum(isinstance(result, ValueError) for result in results) == 1
        assert await db.scalar(select(func.count(PayHotAgreement.id))) == 1
        assert await db.scalar(select(func.count(PayHotPayment.id))) == 1


async def test_first_payment_rechecks_existing_service_composition(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, sub = await seed(db)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel='cabinet'
        )
        sub.connected_squads = ['masked-changed']
        await db.commit()
        assert not await checkout.current_payhot_terms_match(db, sub, q, first=True)


@pytest.mark.parametrize('multi,same_tariff,blocked', [(False, False, True), (True, True, True), (True, False, False)])
async def test_other_provider_pending_target_respects_single_multi_scope(
    postgres_database, monkeypatch, multi, same_tariff, blocked
):
    from app.database.models import CisPaySubscription

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, _ = await seed(db)
        other = Tariff(
            id=2, name='Other', is_active=True, device_limit=2, traffic_limit_gb=10, period_prices={'6': 6000}
        )
        db.add(other)
        await db.flush()
        pending = Subscription(
            user_id=1,
            tariff_id=1 if same_tariff else 2,
            status='expired',
            is_trial=False,
            end_date=datetime.now(UTC),
            remnawave_short_id='pndex1',
        )
        db.add(pending)
        await db.flush()
        db.add(
            CisPaySubscription(
                user_id=1,
                owner_id=1,
                subscription_id=pending.id,
                shop_id='example-shop',
                checkout_id=str(uuid4()),
                status='PENDING',
                snapshot={},
                purpose='example',
            )
        )
        await db.commit()
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: multi)
        if blocked:
            with pytest.raises(ValueError, match='recurrent_already_exists'):
                await checkout.quote_payhot_purchase(
                    db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel='cabinet'
                )
        else:
            assert (
                await checkout.quote_payhot_purchase(
                    db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel='cabinet'
                )
            )['amount_kopeks'] > 0


async def test_future_charge_rejects_manual_squad_change(postgres_database):
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, sub = await seed(db)
        q = await checkout.quote_payhot_purchase(
            db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel='cabinet'
        )
        from app.services.tariff_purchase_quote import load_purchase_quote

        q = await load_purchase_quote(user.id, 'payhot:cabinet', q['quote_id'])
        sub.connected_squads = ['masked-manual']
        await db.commit()
        assert not await checkout.current_payhot_terms_match(db, sub, q)


async def test_cancelled_unknown_charge_blocks_new_consent(postgres_database):
    from app.database.models import PayHotChargeAttempt

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        user, _, sub = await seed(db)
        agreement = PayHotAgreement(
            user_id=1, owner_id=1, subscription_id=1, checkout_id='old-example', status='CANCELLED', consent_snapshot={}
        )
        db.add(agreement)
        await db.flush()
        db.add(
            PayHotChargeAttempt(
                agreement_id=agreement.id,
                cycle_key='old-cycle',
                expected_end_date=sub.end_date,
                request_payload={},
                idempotency_key='example-old-idempotency',
                merchant_reference='renew-old',
                amount_kopeks=10000,
                status='UNKNOWN',
            )
        )
        await db.commit()
        with pytest.raises(ValueError, match='recurrent_already_exists'):
            await checkout.quote_payhot_purchase(
                db, user=user, tariff_id=1, period_days=6, subscription_id=1, channel='cabinet'
            )
