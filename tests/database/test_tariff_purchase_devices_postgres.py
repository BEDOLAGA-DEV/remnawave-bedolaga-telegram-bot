"""Selected devices: real wallet, subscription and retry behaviour on PostgreSQL."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cabinet.routes.subscription_modules import purchase
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@asynccontextmanager
async def checkout_context(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(purchase.user_cart_service, 'save_user_cart', AsyncMock(return_value=True))
    monkeypatch.setattr(purchase, 'emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.checkout_squad_validation.validate_checkout_squads', AsyncMock())
    monkeypatch.setattr('app.services.yandex_offline_conv_service.store_cid_only', AsyncMock())
    from app.services.subscription_service import SubscriptionService

    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock(return_value=None))
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.services.admin_notification_service.AdminNotificationService.send_subscription_purchase_notification',
        AsyncMock(),
    )
    stored = {}

    async def save(key, value, **kwargs):
        stored[key] = value
        return True

    cache = AsyncMock()
    cache.set.side_effect = save
    cache.get.side_effect = lambda key: stored.get(key)
    monkeypatch.setattr('app.services.tariff_purchase_quote.cache', cache)
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            db.add(User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=100000))
            db.add(
                Tariff(
                    id=7,
                    name='Test',
                    is_active=True,
                    is_daily=False,
                    device_limit=1,
                    device_price_kopeks=10000,
                    max_device_limit=5,
                    traffic_limit_gb=100,
                    period_prices={'30': 30000},
                    allowed_squads=['test-squad'],
                )
            )
            await db.commit()

        async def send(path, body):
            async with maker() as db:
                app = FastAPI()
                app.include_router(purchase.router)
                user = await get_user_by_id(db, 1)
                app.dependency_overrides[purchase.get_current_cabinet_user] = lambda: user
                app.dependency_overrides[purchase.get_cabinet_db] = lambda: db
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    return await client.post(path, json=body)

        yield SimpleNamespace(send=send, maker=maker, stored=stored)


async def order(checkout):
    body = {'tariff_id': 7, 'period_days': 30, 'selected_device_limit': 3}
    response = await checkout.send('/purchase-tariff/preview', body)
    assert response.status_code == 200, response.text
    return body | {'quote_id': response.json()['quote_id'], 'checkout_id': str(uuid4())}


@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('classic_override', [False, True])
async def test_checkout_prices_and_provisions_once(postgres_database, monkeypatch, concurrent, classic_override):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', not classic_override)
        monkeypatch.setattr(settings, 'DEVICES_SELECTION_DISABLED_AMOUNT', 2 if classic_override else None)
        body = await order(checkout)
        if concurrent:
            responses = await asyncio.gather(*(checkout.send('/purchase-tariff', body) for _ in range(2)))
        else:
            first = await checkout.send('/purchase-tariff', body)
            checkout.stored.clear()  # A completed retry must not depend on a Redis quote.
            responses = [first, await checkout.send('/purchase-tariff', body)]
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 50000
            subscriptions = (await db.scalars(select(Subscription))).all()
            assert len(subscriptions) == 1
            assert subscriptions[0].device_limit == 3
            from app.services.panel_sync import build_panel_payload

            await db.refresh(subscriptions[0], ['tariff'])
            payload = build_panel_payload(await db.get(User, 1), subscriptions[0], multi_tariff=True)
            assert payload.create_kwargs()['hwid_device_limit'] == 3
            assert payload.update_kwargs(user_id=9001)['hwid_device_limit'] == 3
            payments = (await db.scalars(select(Transaction))).all()
            assert len(payments) == 1
            assert abs(payments[0].amount_kopeks) == 50000


async def test_quote_changed_before_debit(postgres_database, monkeypatch):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        body = await order(checkout)
        async with checkout.maker() as db:
            tariff = await db.get(Tariff, 7)
            tariff.device_price_kopeks = 20000
            await db.commit()
        response = await checkout.send('/purchase-tariff', body)
        assert response.status_code == 409, response.text
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 100000
            assert (await db.scalars(select(Transaction))).all() == []


async def test_reused_checkout_with_different_devices_rejected(postgres_database, monkeypatch):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        body = await order(checkout)
        assert (await checkout.send('/purchase-tariff', body)).status_code == 200
        response = await checkout.send('/purchase-tariff', body | {'selected_device_limit': 4})
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'checkout_conflict'


async def test_provision_failure_rolls_back_debit_and_allows_retry(postgres_database, monkeypatch):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        body = await order(checkout)
        original = purchase.create_paid_subscription
        monkeypatch.setattr(
            purchase, 'create_paid_subscription', AsyncMock(side_effect=RuntimeError('injected failure'))
        )
        assert (await checkout.send('/purchase-tariff', body)).status_code == 500
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 100000
            assert (await db.scalars(select(Transaction))).all() == []
        monkeypatch.setattr(purchase, 'create_paid_subscription', original)
        assert (await checkout.send('/purchase-tariff', body)).status_code == 200


async def test_selected_devices_require_quote_and_checkout_id(postgres_database, monkeypatch):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        response = await checkout.send(
            '/purchase-tariff', {'tariff_id': 7, 'period_days': 30, 'selected_device_limit': 3}
        )
        assert response.status_code == 422


@pytest.mark.parametrize('changed', [False, True])
async def test_topup_preserves_devices_and_agreed_price(postgres_database, monkeypatch, changed):
    from app.services import subscription_auto_purchase_service as auto

    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        body = await order(checkout)
        async with checkout.maker() as db:
            user = await db.get(User, 1)
            user.balance_kopeks = 0
            await db.commit()
        response = await checkout.send('/purchase-tariff', body)
        assert response.status_code == 402
        cart = purchase.user_cart_service.save_user_cart.await_args.args[1]
        assert cart['selected_device_limit'] == 3
        async with checkout.maker() as db:
            user = await get_user_by_id(db, 1)
            user.balance_kopeks = 100000
            if changed:
                (await db.get(Tariff, 7)).device_price_kopeks = 20000
            await db.commit()
            succeeded = await auto._auto_purchase_tariff(db, user, cart)
            assert succeeded is (not changed)
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == (100000 if changed else 50000)
            subs = (await db.scalars(select(Subscription))).all()
            assert len(subs) == (0 if changed else 1)
            if subs:
                assert subs[0].device_limit == 3
        if not changed:
            checkout.stored.clear()
            assert (await checkout.send('/purchase-tariff', body)).status_code == 200


@pytest.mark.parametrize('multi', [False, True])
async def test_trial_conversion_receives_selected_limit(postgres_database, monkeypatch, multi):
    from datetime import UTC, datetime, timedelta

    async with checkout_context(postgres_database, monkeypatch) as checkout:
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
        async with checkout.maker() as db:
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=7,
                    is_trial=True,
                    status='active',
                    device_limit=1,
                    end_date=datetime.now(UTC) + timedelta(days=2),
                )
            )
            await db.commit()
        body = await order(checkout)
        response = await checkout.send('/purchase-tariff', body)
        assert response.status_code == 200, response.text
        async with checkout.maker() as db:
            subs = (await db.scalars(select(Subscription))).all()
            assert len(subs) == 1
            assert subs[0].id == 10 and not subs[0].is_trial
            assert subs[0].device_limit == 3


async def test_manual_and_topup_race_charge_once(postgres_database, monkeypatch):
    from app.services import subscription_auto_purchase_service as auto

    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        body = await order(checkout)
        async with checkout.maker() as db:
            (await db.get(User, 1)).balance_kopeks = 0
            await db.commit()
        assert (await checkout.send('/purchase-tariff', body)).status_code == 402
        cart = purchase.user_cart_service.save_user_cart.await_args.args[1]
        async with checkout.maker() as db:
            (await db.get(User, 1)).balance_kopeks = 100000
            await db.commit()

        async def topup():
            async with checkout.maker() as db:
                user = await get_user_by_id(db, 1)
                await auto._auto_purchase_tariff(db, user, cart)

        response, _ = await asyncio.gather(checkout.send('/purchase-tariff', body), topup())
        assert response.status_code == 200, response.text
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 50000
            assert len((await db.scalars(select(Transaction))).all()) == 1
            assert (await db.scalar(select(Subscription))).device_limit == 3


async def test_preview_resolves_trial_of_another_tariff_before_consent(postgres_database, monkeypatch):
    from datetime import UTC, datetime, timedelta

    async with checkout_context(postgres_database, monkeypatch) as checkout:
        async with checkout.maker() as db:
            db.add(Tariff(id=8, name='Trial', is_active=True, device_limit=1))
            await db.flush()
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=8,
                    is_trial=True,
                    status='active',
                    device_limit=1,
                    end_date=datetime.now(UTC) + timedelta(days=2),
                )
            )
            await db.commit()
        body = {'tariff_id': 7, 'period_days': 30, 'selected_device_limit': 3}
        preview = await checkout.send('/purchase-tariff/preview', body)
        assert preview.status_code == 200
        assert preview.json()['purchase_kind'] == 'trial_conversion'
        assert preview.json()['subscription_id'] == 10
        response = await checkout.send(
            '/purchase-tariff', body | {'quote_id': preview.json()['quote_id'], 'checkout_id': str(uuid4())}
        )
        assert response.status_code == 200, response.text
        async with checkout.maker() as db:
            sub = await db.get(Subscription, 10)
            assert sub.tariff_id == 7 and sub.device_limit == 3 and not sub.is_trial


@pytest.mark.parametrize('change', ['inactive', 'restricted', 'classic'])
async def test_completed_retry_survives_later_availability_change(postgres_database, monkeypatch, change):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        body = await order(checkout)
        assert (await checkout.send('/purchase-tariff', body)).status_code == 200
        async with checkout.maker() as db:
            if change == 'inactive':
                (await db.get(Tariff, 7)).is_active = False
            elif change == 'restricted':
                (await db.get(User, 1)).restriction_subscription = True
            else:
                monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
            await db.commit()
        checkout.stored.clear()
        assert (await checkout.send('/purchase-tariff', body)).status_code == 200
        assert (await checkout.send('/purchase-tariff', body | {'selected_device_limit': 4})).status_code == 409


@pytest.mark.parametrize('trial_status', ['active', 'disabled'])
async def test_telegram_trial_other_tariff_cart_reaches_real_topup_dispatcher(
    postgres_database, monkeypatch, trial_status
):
    import inspect
    from datetime import UTC, datetime, timedelta

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.handlers.subscription import tariff_purchase as telegram
    from app.services import subscription_auto_purchase_service as auto

    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        async with checkout.maker() as db:
            user = await get_user_by_id(db, 1)
            user.balance_kopeks = 0
            db.add(Tariff(id=8, name='Trial', is_active=True, device_limit=1))
            await db.flush()
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=7 if trial_status == 'disabled' else 8,
                    is_trial=True,
                    status=trial_status,
                    device_limit=1,
                    end_date=datetime.now(UTC) + timedelta(days=2),
                )
            )
            await db.commit()
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
            await state.update_data(tariff_device_selection={'tariff_id': 7, 'limit': 3})
            callback = SimpleNamespace(
                data='tariff_period:7:30', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
            )
            await inspect.unwrap(telegram.select_tariff_period)(callback, user, db, state)
            cart = purchase.user_cart_service.save_user_cart.await_args.args[1]
            assert cart['subscription_id'] == 10
            user.balance_kopeks = 100000
            await db.commit()
            assert await auto._process_single_cart(db, user, cart)
        async with checkout.maker() as db:
            sub = await db.get(Subscription, 10)
            assert sub.tariff_id == 7 and sub.device_limit == 3 and not sub.is_trial
            assert (await db.get(User, 1)).balance_kopeks == 50000


async def test_legacy_request_rejects_date_overflow_before_any_ledger_write(postgres_database, monkeypatch):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        async with checkout.maker() as db:
            (await db.get(Tariff, 7)).period_prices = {'2147483647': 1000}
            await db.commit()
        response = await checkout.send('/purchase-tariff', {'tariff_id': 7, 'period_days': 2147483647})
        assert response.status_code == 409, response.text
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 100000
            assert (await db.scalars(select(Transaction))).all() == []


async def test_cart_storage_failure_does_not_claim_order_saved(postgres_database, monkeypatch):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        body = await order(checkout)
        async with checkout.maker() as db:
            (await db.get(User, 1)).balance_kopeks = 0
            await db.commit()
        purchase.user_cart_service.save_user_cart.return_value = False
        response = await checkout.send('/purchase-tariff', body)
        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'cart_unavailable'


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('topup', [False, True])
@pytest.mark.parametrize('trial', [False, True])
async def test_daily_selected_devices_persist_with_charge_policy(postgres_database, monkeypatch, multi, topup, trial):
    from app.services import subscription_auto_purchase_service as auto
    from app.services.pricing_engine import PricingEngine

    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
        async with checkout.maker() as db:
            tariff = await db.get(Tariff, 7)
            tariff.is_daily = True
            tariff.daily_price_kopeks = 1000
            tariff.device_price_kopeks = 3000
            (await db.get(User, 1)).balance_kopeks = 0 if topup else 100000
            if trial:
                from datetime import UTC, datetime, timedelta

                db.add(
                    Subscription(
                        id=10,
                        user_id=1,
                        tariff_id=7,
                        is_trial=True,
                        status='active',
                        device_limit=1,
                        end_date=datetime.now(UTC) + timedelta(days=1),
                    )
                )
            await db.commit()
        parameters = {'tariff_id': 7, 'period_days': 1, 'selected_device_limit': 3}
        preview = await checkout.send('/purchase-tariff/preview', parameters)
        assert preview.status_code == 200, preview.text
        body = parameters | {'quote_id': preview.json()['quote_id'], 'checkout_id': str(uuid4())}
        if topup:
            response = await checkout.send('/purchase-tariff', body)
            assert response.status_code == 402, response.text
            cart = purchase.user_cart_service.save_user_cart.await_args.args[1]
            assert cart['period_days'] == 1
            async with checkout.maker() as db:
                user = await get_user_by_id(db, 1)
                user.balance_kopeks = 100000
                await db.commit()
                assert await auto._process_single_cart(db, user, cart)
        else:
            responses = await asyncio.gather(*(checkout.send('/purchase-tariff', body) for _ in range(2)))
            assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        async with checkout.maker() as db:
            user = await get_user_by_id(db, 1)
            assert user.balance_kopeks == 98800
            sub = (await db.scalars(select(Subscription))).one()
            tariff = await db.get(Tariff, 7)
            assert sub.device_limit == 3
            assert sub.daily_devices_billing_version == 1
            assert sub.last_daily_charge_at is not None
            assert not sub.is_daily_paused
            assert PricingEngine.daily_subscription_price(sub, tariff, user) == 1200
        assert (await checkout.send('/purchase-tariff', body)).status_code == 200


async def test_daily_selected_intent_failure_rolls_back_wallet_and_product(postgres_database, monkeypatch):
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        async with checkout.maker() as db:
            tariff = await db.get(Tariff, 7)
            tariff.is_daily = True
            tariff.daily_price_kopeks = 1000
            tariff.device_price_kopeks = 3000
            await db.commit()
        parameters = {'tariff_id': 7, 'period_days': 1, 'selected_device_limit': 3}
        preview = await checkout.send('/purchase-tariff/preview', parameters)
        assert preview.status_code == 200, preview.text
        body = parameters | {'quote_id': preview.json()['quote_id'], 'checkout_id': str(uuid4())}
        from sqlalchemy import event

        from app.database.models import TariffPurchaseIntent

        def fail_intent(*args):
            raise RuntimeError('synthetic intent insert failure')

        event.listen(TariffPurchaseIntent, 'before_insert', fail_intent)
        try:
            response = await checkout.send('/purchase-tariff', body)
            assert response.status_code == 500, response.text
        finally:
            event.remove(TariffPurchaseIntent, 'before_insert', fail_intent)
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 100000
            assert (await db.scalars(select(Subscription))).all() == []
            assert (await db.scalars(select(Transaction))).all() == []


async def test_existing_versioned_daily_legacy_purchase_keeps_monthly_proration(postgres_database, monkeypatch):
    from datetime import UTC, datetime, timedelta

    async with checkout_context(postgres_database, monkeypatch) as checkout:
        async with checkout.maker() as db:
            tariff = await db.get(Tariff, 7)
            tariff.is_daily = True
            tariff.daily_price_kopeks = 1000
            tariff.device_price_kopeks = 3000
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=7,
                    is_trial=False,
                    status='active',
                    device_limit=3,
                    daily_devices_billing_version=1,
                    is_daily_paused=True,
                    end_date=datetime.now(UTC) - timedelta(hours=1),
                )
            )
            await db.commit()
        response = await checkout.send('/purchase-tariff', {'tariff_id': 7, 'period_days': 1, 'subscription_id': 10})
        assert response.status_code == 200, response.text
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 98800


@pytest.mark.parametrize('expired', [False, True])
async def test_legacy_miniapp_purchase_prices_saved_daily_device_policy(postgres_database, monkeypatch, expired):
    from datetime import UTC, datetime, timedelta

    from app.webapi.routes import miniapp

    async with checkout_context(postgres_database, monkeypatch) as checkout:
        async with checkout.maker() as db:
            tariff = await db.get(Tariff, 7)
            tariff.is_daily = True
            tariff.daily_price_kopeks = 1000
            tariff.device_price_kopeks = 3000
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=7,
                    is_trial=False,
                    status='expired' if expired else 'active',
                    device_limit=3,
                    daily_devices_billing_version=1,
                    is_daily_paused=True,
                    end_date=datetime.now(UTC) + timedelta(days=-1 if expired else 1),
                )
            )
            await db.commit()
        async with checkout.maker() as db:
            user = await get_user_by_id(db, 1)
            monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
            await miniapp.purchase_tariff_endpoint(
                miniapp.MiniAppTariffPurchaseRequest(initData='test', tariffId=7, periodDays=1), db
            )
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == 98800
            assert (await db.get(Subscription, 10)).daily_devices_billing_version == 1


@pytest.mark.parametrize('flow', ['telegram', 'auto'])
async def test_legacy_daily_conversion_does_not_inherit_old_device_policy(postgres_database, monkeypatch, flow):
    import inspect
    from datetime import UTC, datetime, timedelta

    from app.handlers.subscription import tariff_purchase as telegram
    from app.services import subscription_auto_purchase_service as auto
    from tests.handlers.test_tariff_purchase_device_selection import context

    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(telegram, 'should_create_panel_account', AsyncMock(return_value=True))
    monkeypatch.setattr(telegram.user_cart_service, 'delete_user_cart', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_subscription_cart', AsyncMock())
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
        async with checkout.maker() as db:
            tariff = await db.get(Tariff, 7)
            tariff.is_daily = True
            tariff.daily_price_kopeks = 1000
            db.add(Tariff(id=8, name='Old', is_active=True, is_daily=True, daily_price_kopeks=500, device_limit=1))
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=8,
                    is_trial=False,
                    status='expired',
                    device_limit=3,
                    daily_devices_billing_version=1,
                    is_daily_paused=True,
                    end_date=datetime.now(UTC) - timedelta(days=1),
                )
            )
            await db.commit()
        async with checkout.maker() as db:
            user = await get_user_by_id(db, 1)
            if flow == 'auto':
                assert await auto._auto_purchase_daily_tariff(
                    db, user, {'tariff_id': 7, 'subscription_id': 10, 'cart_mode': 'daily_tariff_purchase'}
                )
            else:
                state = context()
                await state.update_data(target_subscription_id=10)
                callback = SimpleNamespace(
                    data='daily_tariff_confirm:7',
                    answer=AsyncMock(),
                    bot=SimpleNamespace(send_message=AsyncMock()),
                    message=SimpleNamespace(edit_text=AsyncMock()),
                )
                await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
        async with checkout.maker() as db:
            sub = await db.get(Subscription, 10)
            assert sub.tariff_id == 7
            assert sub.daily_devices_billing_version == 0
            assert (await db.get(User, 1)).balance_kopeks == 99000


@pytest.mark.parametrize('price_changed', [False, True])
async def test_admin_daily_renewal_preserves_devices_it_charges(postgres_database, monkeypatch, price_changed):
    import inspect
    from datetime import UTC, datetime, timedelta

    from app.handlers.admin import users as admin

    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    async with checkout_context(postgres_database, monkeypatch) as checkout:
        async with checkout.maker() as db:
            tariff = await db.get(Tariff, 7)
            tariff.is_daily = True
            tariff.daily_price_kopeks = 1000
            tariff.device_price_kopeks = 3000
            tariff.period_prices = {}
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=7,
                    is_trial=False,
                    status='active',
                    device_limit=3,
                    daily_devices_billing_version=1,
                    end_date=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await db.commit()
        async with checkout.maker() as db:
            user = await get_user_by_id(db, 1)
            monkeypatch.setattr(admin.UserService, 'get_user_profile', AsyncMock(return_value={'user': user}))
            callback = SimpleNamespace(
                data='admin_buy_tariff_execute_1_7_1_1200',
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock()),
                bot=SimpleNamespace(send_message=AsyncMock()),
            )
            callback.data = 'admin_tariff_buy_select_1_7'
            await inspect.unwrap(admin.admin_buy_tariff_period)(callback, user, db)
            buttons = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
            assert buttons[0][0].callback_data == 'admin_tariff_buy_confirm_1_7_1_1200'
            callback.data = buttons[0][0].callback_data
            await inspect.unwrap(admin.admin_buy_tariff_confirm)(callback, user, db)
            confirmation = callback.message.edit_text.await_args
            assert '📱 Устройств: 3' in confirmation.args[0]
            callback.data = confirmation.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            assert callback.data.endswith('_1200')
            if price_changed:
                (await db.get(Tariff, 7)).device_price_kopeks = 6000
                await db.commit()
            await inspect.unwrap(admin.admin_buy_tariff_execute)(callback, user, db)
            if not price_changed:
                assert '📱 Устройств: 3' in callback.message.edit_text.await_args.args[0]
                assert '📱 Устройств: 3' in callback.bot.send_message.await_args.kwargs['text']
        async with checkout.maker() as db:
            assert (await db.get(User, 1)).balance_kopeks == (100000 if price_changed else 98800)
            assert (await db.get(Subscription, 10)).device_limit == 3
