"""AuraPay subscription charges must extend once per signed invoice."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import (
    AuraPaySubscription,
    CasheraSubscription,
    LavaSubscription,
    PlategaSubscription,
    Subscription,
    Tariff,
    Transaction,
    User,
)
from app.services.aurapay_recurrent import enable, process_event, resolve_interval
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.parametrize(
    ('days', 'expected'),
    [(1, (1, 'day')), (7, (7, 'day')), (30, (1, 'month')), (90, (3, 'month')), (365, (1, 'year'))],
)
def test_interval_matches_tariff_period(days, expected):
    assert resolve_interval(days) == expected


async def test_enable_creates_card_plan_with_only_required_contact(monkeypatch):
    for key, value in {
        'AURAPAY_ENABLED': True,
        'AURAPAY_RECURRENT_ENABLED': True,
        'AURAPAY_API_KEY': 'test-key',
        'AURAPAY_SHOP_ID': 'test-shop',
        'AURAPAY_SECRET_KEY': 'test-secret',
        'WEBHOOK_URL': 'https://example.invalid',
    }.items():
        monkeypatch.setattr(settings, key, value)
    create = AsyncMock(
        return_value={
            'subscription': {'id': 'provider-sub'},
            'payment_data': {'url': 'https://example.invalid/pay'},
        }
    )
    monkeypatch.setattr('app.services.aurapay_recurrent.aurapay_service.create_subscription', create)
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=10000)),
    )

    tables = (
        User.__table__,
        Tariff.__table__,
        Subscription.__table__,
        AuraPaySubscription.__table__,
        PlategaSubscription.__table__,
        LavaSubscription.__table__,
        CasheraSubscription.__table__,
    )
    async with memory_session(monkeypatch, tables) as db:
        tariff = Tariff(name='Test', is_active=True, period_prices={'30': 10000})
        db.add(tariff)
        await db.flush()
        user = User(telegram_id=tariff.id, balance_kopeks=0)
        db.add(user)
        await db.flush()
        subscription = Subscription(
            user_id=user.id,
            tariff_id=tariff.id,
            is_trial=False,
            status='active',
            start_date=datetime.now(UTC),
            end_date=datetime.now(UTC),
            remnawave_short_id='auratest2',
            device_limit=1,
            autopay_enabled=True,
        )
        db.add(subscription)
        await db.commit()

        record = await enable(db, user=user, subscription=subscription, tariff=tariff)
        payload = create.await_args.args[0]
        assert payload['identifier_type'] == 'TG_CHAT_ID'
        assert payload['identifier'] == str(user.telegram_id)
        assert payload['amount'] == 100.0
        assert payload['interval'] == 'month'
        assert payload['service'] == 'card'
        assert 'email' not in payload
        assert 'custom_fields' not in payload
        assert record.provider_id == 'provider-sub'
        assert subscription.autopay_enabled is False
        assert await enable(db, user=user, subscription=subscription, tariff=tariff) is record
        create.assert_awaited_once()


async def test_paid_invoice_extends_once_and_rejects_wrong_amount(monkeypatch):
    monkeypatch.setattr(settings, 'AURAPAY_SHOP_ID', 'test-shop')
    monkeypatch.setattr(
        'app.database.crud.subscription.reconcile_tariff_traffic_limit',
        AsyncMock(),
    )
    monkeypatch.setattr('app.services.grace_access_echo.undo_grace_overlay_echo', AsyncMock())
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.update_remnawave_user',
        AsyncMock(),
    )

    tables = (
        User.__table__,
        Tariff.__table__,
        Subscription.__table__,
        AuraPaySubscription.__table__,
        Transaction.__table__,
    )
    async with memory_session(monkeypatch, tables) as db:
        tariff = Tariff(name='Test', is_active=True, period_prices={'30': 10000})
        db.add(tariff)
        await db.flush()
        user = User(telegram_id=tariff.id, balance_kopeks=0)
        db.add(user)
        await db.flush()
        subscription = Subscription(
            user_id=user.id,
            tariff_id=tariff.id,
            is_trial=False,
            status='expired',
            start_date=datetime.now(UTC),
            end_date=datetime.now(UTC),
            remnawave_short_id='auratest',
        )
        db.add(subscription)
        await db.flush()
        record = AuraPaySubscription(
            user_id=user.id,
            subscription_id=subscription.id,
            merchant_id='test-sub',
            provider_id='test-provider',
            amount_kopeks=10000,
            charge_days=30,
            period=1,
            interval='month',
            status='NEW',
        )
        db.add(record)
        await db.commit()

        event = {
            'event': 'ACTIVATED',
            'subscription_id': 'test-sub',
            'id': 'test-provider',
            'shop_id': 'test-shop',
            'invoice_id': 'invoice-1',
            'amount': '100.00',
            'status': 'ACTIVE',
        }
        assert await process_event(db, {**event, 'amount': '99.00'}) is False
        assert await process_event(db, event) is True
        first_end = subscription.end_date
        assert await process_event(db, event) is True
        assert subscription.end_date == first_end

        assert await process_event(db, {**event, 'event': 'PAID', 'invoice_id': 'invoice-2'}) is True
        assert subscription.end_date > first_end
        transactions = (await db.execute(select(Transaction))).scalars().all()
        assert {tx.external_id for tx in transactions} == {'invoice-1', 'invoice-2'}
