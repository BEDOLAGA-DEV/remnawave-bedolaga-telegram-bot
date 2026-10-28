"""Selected-device recurrent checkout persists consent before provider setup."""

from datetime import UTC, datetime, timedelta
from importlib import import_module
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, Tariff, TariffPurchaseIntent, User
from app.services import recurrent_purchase_selection as service
from tests.database.test_recurrent_purchase_selected_subscription_postgres import PROVIDERS
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(('provider', 'purchase_name', 'enable_name'), PROVIDERS)
@pytest.mark.parametrize(
    'case',
    [
        'new',
        'retry',
        'changed_selection',
        'paid',
        'trial',
        'foreign',
        'changed_price',
        'daily',
        'provider_failure',
        'provider_value_error',
        'recovery_detail',
        'terminal',
    ],
)
async def test_selected_device_checkout(postgres_database, monkeypatch, provider, purchase_name, enable_name, case):
    module = import_module(f'app.services.payment.{provider}')
    purchase = getattr(module, f'purchase_tariff_with_{purchase_name}_recurring')
    monkeypatch.setattr(type(settings), f'is_{provider}_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(
        service,
        'resolve_lava_purchase_product',
        AsyncMock(return_value={'price': '500', 'periodDays': 30, 'period': 'one_month'}),
        raising=False,
    )
    from app.services.tariff_purchase_quote import cache

    saved = {}

    async def save(key, value, **kw):
        saved[key] = value
        return True

    monkeypatch.setattr(cache, 'set', save)
    monkeypatch.setattr(cache, 'get', AsyncMock(side_effect=lambda key: saved.get(key)))
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=101, balance_kopeks=10000)
        tariff = Tariff(
            id=1,
            name='Example',
            is_active=True,
            device_limit=1,
            device_price_kopeks=10000,
            max_device_limit=5,
            period_prices={'30': 30000},
            allowed_squads=['example'],
            allowed_promo_groups=[],
            lava_product_id='example',
        )
        db.add_all([user, User(id=2, telegram_id=202), tariff])
        await db.commit()
        sid = None
        if case in ('paid', 'trial', 'foreign'):
            sub = Subscription(
                user_id=2 if case == 'foreign' else 1,
                tariff_id=1,
                device_limit=2,
                status='expired',
                is_trial=case == 'trial',
                start_date=datetime.now(UTC) - timedelta(days=60),
                end_date=datetime.now(UTC) - timedelta(days=30),
            )
            db.add(sub)
            await db.commit()
            sid = sub.id
        enable = AsyncMock(return_value={'status': 'pending', 'redirect_url': 'https://payment.example/checkout'})
        monkeypatch.setattr(module, enable_name, enable)
        if sid is not None:
            with pytest.raises(ValueError):
                await service.preview_recurrent_purchase(
                    db, user=user, tariff=tariff, provider=provider, selected_device_limit=3, subscription_id=sid
                )
            assert sub.device_limit == 2
            enable.assert_not_awaited()
            return
        if case == 'daily':
            tariff.is_daily = True
            tariff.daily_price_kopeks = 1000
            tariff.device_price_kopeks = 100
            await db.commit()
        preview = await service.preview_recurrent_purchase(
            db, user=user, tariff=tariff, provider=provider, selected_device_limit=3
        )
        args = dict(
            user=user, tariff=tariff, selected_device_limit=3, quote_id=preview['quote_id'], checkout_id=uuid4()
        )
        if case == 'changed_price':
            tariff.device_price_kopeks = 11000
            await db.commit()
            with pytest.raises(ValueError):
                await purchase(db, **args)
            assert not (await db.scalars(select(Subscription))).all()
            enable.assert_not_awaited()
            return
        if case in ('provider_failure', 'provider_value_error', 'recovery_detail'):
            failure = ValueError if case == 'provider_value_error' else RuntimeError
            enable.side_effect = failure('example setup failure')
            with pytest.raises(failure):
                await purchase(db, **args)
            enable.side_effect = None
            saved.clear()
            if case == 'recovery_detail':
                args['subscription_id'] = await db.scalar(select(Subscription.id))
            restored = await service.preview_recurrent_purchase(
                db,
                user=user,
                tariff=tariff,
                provider=provider,
                selected_device_limit=3,
                subscription_id=args.get('subscription_id'),
            )
            assert restored['checkout_id'] == str(args['checkout_id'])
            args['quote_id'] = restored['quote_id']
        result = await purchase(db, **args)
        subscription = await db.get(Subscription, result['subscription_id'])
        assert subscription.device_limit == 3
        assert subscription.status == 'expired'
        assert subscription.daily_devices_billing_version == (1 if case == 'daily' else 0)
        assert enable.await_args.kwargs['subscription'].device_limit == 3
        assert len((await db.scalars(select(TariffPurchaseIntent))).all()) == 1
        if case in ('retry', 'terminal'):
            saved.clear()  # durable retry survives quote expiry and payment success
            subscription.status = 'active'
            subscription.end_date = datetime.now(UTC) + timedelta(days=30)
            from app.database.models import CasheraSubscription, LavaSubscription, PlategaSubscription

            model = {'platega': PlategaSubscription, 'lava': LavaSubscription, 'cashera': CasheraSubscription}[provider]
            extras = {
                'platega': {'interval': 3},
                'lava': {'lava_product_id': 'example', 'order_id': 'example'},
                'cashera': {'interval': 'monthly', 'external_id': 'example'},
            }[provider]
            db.add(
                model(
                    user_id=1,
                    subscription_id=subscription.id,
                    tariff_id=1,
                    charge_days=30,
                    amount_kopeks=50000,
                    status='CANCELLED' if case == 'terminal' else 'ACTIVE',
                    **extras,
                )
            )
            await db.commit()
            if case == 'terminal':
                with pytest.raises(ValueError, match='checkout_closed'):
                    await purchase(db, **args)
                assert enable.await_count == 1
                return
            result2 = await purchase(db, **args)
            assert result2['subscription_id'] == subscription.id
        if case == 'changed_selection':
            args['selected_device_limit'] = 4
            with pytest.raises(ValueError):
                await purchase(db, **args)
        assert len((await db.scalars(select(Subscription))).all()) == 1
        assert subscription.device_limit == 3
        assert user.balance_kopeks == 10000


async def test_concurrent_recurrent_checkout_creates_one_bound_row(postgres_database, monkeypatch):
    import asyncio

    from app.database.crud.tariff import get_tariff_by_id
    from app.database.crud.user import get_user_by_id
    from app.services.tariff_purchase_quote import cache
    from tests.fixtures.postgres_db import postgres_sessions

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    saved = {}

    async def save(key, value, **kw):
        saved[key] = value
        return True

    monkeypatch.setattr(cache, 'set', save)
    monkeypatch.setattr(cache, 'get', AsyncMock(side_effect=lambda key: saved.get(key)))
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (left, right):
        left.add_all(
            [
                User(id=1, telegram_id=101),
                Tariff(
                    id=1,
                    name='Example',
                    is_active=True,
                    device_limit=1,
                    device_price_kopeks=10000,
                    max_device_limit=5,
                    period_prices={'30': 30000},
                    allowed_squads=['example'],
                ),
            ]
        )
        await left.commit()
        user = await get_user_by_id(left, 1)
        tariff = await get_tariff_by_id(left, 1)
        preview = await service.preview_recurrent_purchase(
            left, user=user, tariff=tariff, provider='platega', selected_device_limit=3
        )
        await left.commit()
        checkout = uuid4()

        async def prepare(db):
            return await service.prepare_recurrent_device_purchase(
                db,
                user=await get_user_by_id(db, 1),
                tariff=await get_tariff_by_id(db, 1),
                provider='platega',
                selected_device_limit=3,
                quote_id=preview['quote_id'],
                checkout_id=checkout,
            )

        a, b = await asyncio.gather(prepare(left), prepare(right))
        assert a.id == b.id
        assert a.device_limit == b.device_limit == 3
        assert len((await left.scalars(select(Subscription))).all()) == 1
        assert len((await left.scalars(select(TariffPurchaseIntent))).all()) == 1


@pytest.mark.parametrize(('provider', 'purchase_name', 'enable_name'), PROVIDERS)
async def test_real_provider_setup_stores_selected_device_amount(
    postgres_database, monkeypatch, provider, purchase_name, enable_name
):
    from types import SimpleNamespace

    from app.database.models import CasheraSubscription, LavaSubscription, PlategaSubscription
    from app.services.tariff_purchase_quote import cache

    module = import_module(f'app.services.payment.{provider}')
    monkeypatch.setattr(type(settings), f'is_{provider}_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example')
    for name in (
        'app.services.payment.lava.cancel_lava_recurring_for_subscription_safe',
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        'app.services.cashera_recurring_cancel.cancel_cashera_recurring_for_subscription_safe',
    ):
        monkeypatch.setattr(name, AsyncMock())
    product = {'price': '500', 'periodDays': 30, 'period': 'one_month'}
    monkeypatch.setattr(service, 'resolve_lava_purchase_product', AsyncMock(return_value=product))
    if provider == 'platega':
        remote = SimpleNamespace(
            create_subscription=AsyncMock(
                return_value={'transactionId': 'example', 'redirect': 'https://pay.example', 'status': 'PENDING'}
            )
        )
        monkeypatch.setattr(module, 'PlategaService', lambda: remote)
    elif provider == 'lava':
        remote = SimpleNamespace(
            create_recurrent_consumer=AsyncMock(),
            subscribe_recurrent=AsyncMock(
                return_value={'data': {'subscriptionId': 'example', 'url': 'https://pay.example', 'amount': 500}}
            ),
        )
        monkeypatch.setattr(module, 'lava_service', remote)
        monkeypatch.setattr(module._LavaRecurrentAgent, '_resolve_lava_product', AsyncMock(return_value=product))
    else:
        remote = SimpleNamespace(
            create_subscription=AsyncMock(
                return_value={'uuid': 'example', 'payment_url': 'https://pay.example', 'status': 'pending_agreement'}
            )
        )
        monkeypatch.setattr(module, 'cashera_service', remote)
    saved = {}

    async def save(key, value, **kw):
        saved[key] = value
        return True

    monkeypatch.setattr(cache, 'set', save)
    monkeypatch.setattr(cache, 'get', AsyncMock(side_effect=lambda key: saved.get(key)))
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=101)
        tariff = Tariff(
            id=1,
            name='Example',
            is_active=True,
            device_limit=1,
            device_price_kopeks=10000,
            max_device_limit=5,
            period_prices={'30': 30000},
            allowed_squads=['example'],
            allowed_promo_groups=[],
            lava_product_id='example',
        )
        db.add_all([user, tariff])
        await db.commit()
        preview = await service.preview_recurrent_purchase(
            db, user=user, tariff=tariff, provider=provider, selected_device_limit=3
        )
        result = await getattr(module, f'purchase_tariff_with_{purchase_name}_recurring')(
            db, user=user, tariff=tariff, selected_device_limit=3, quote_id=preview['quote_id'], checkout_id=uuid4()
        )
        model = {'platega': PlategaSubscription, 'lava': LavaSubscription, 'cashera': CasheraSubscription}[provider]
        record = (await db.scalars(select(model))).one()
        assert record.amount_kopeks == preview['amount_kopeks'] == 50000
        assert record.charge_days == preview['charge_days'] == 30
        assert (await db.get(Subscription, result['subscription_id'])).device_limit == 3
