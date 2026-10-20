"""Classic subscriptions must retain and pay for carried devices on tariff purchase."""

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.external.remnawave_api import RemnaWaveAPI
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class Panel(RemnaWaveAPI):
    def __init__(self, now):
        super().__init__(base_url='https://panel.example', api_key='test')
        self.patches = []
        self.account = dict(
            id=9001,
            shortUuid='test-classic',
            username='test-classic',
            expireAt=(now + timedelta(days=10)).isoformat(),
            status='ACTIVE',
            createdAt=now.isoformat(),
            updatedAt=now.isoformat(),
            telegramId=101,
            subscriptionUrl='https://vpn.example/sub/test',
            hwidDeviceLimit=3,
        )

    async def get_user_by_id(self, user_id):
        assert user_id == 9001
        return self._parse_user(self.account)

    async def enrich_user_with_happ_link(self, user):
        return user

    async def _make_request(self, method, path, data=None, **kwargs):
        if (method, path) == ('PATCH', '/api/users'):
            assert data['id'] == 9001
            self.patches.append(data.copy())
            self.account.update(data)
            return {'response': self.account}
        assert (method, path) == ('POST', '/api/users/9001/actions/reset-traffic')
        return {'response': self.account}


@pytest.mark.parametrize('flow', ['cabinet', 'telegram', 'auto'])
@pytest.mark.parametrize('multi', [False, True])
async def test_selected_classic_devices_are_priced_and_preserved(
    postgres_database,
    monkeypatch,
    flow,
    multi,
    days=30,
    base=1,
    old=3,
    tariff_max=None,
    global_max=10,
    expected=3,
    daily=False,
    repeat=False,
    trial=False,
    custom_traffic=None,
    custom_enabled=False,
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', global_max)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_user_cart', AsyncMock())
    monkeypatch.setattr(telegram.user_cart_service, 'delete_subscription_cart', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    trial_notice = AsyncMock()
    if trial and flow == 'telegram_cart_period':

        async def notify(callback):
            await callback(SimpleNamespace(send_subscription_purchase_notification=trial_notice))

        monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', notify)

    monkeypatch.setattr(telegram.AdminNotificationService, 'send_subscription_purchase_notification', AsyncMock())
    now = datetime.now(UTC)
    panel = Panel(now)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(
                    id=1,
                    telegram_id=101,
                    language='ru',
                    status='active',
                    balance_kopeks=10000,
                    remnawave_id=9002 if multi else 9001,
                ),
                User(id=2, telegram_id=202),
            ]
        )
        tariff = Tariff(
            id=1,
            name='Test',
            is_active=True,
            traffic_limit_gb=100,
            custom_traffic_enabled=custom_traffic is not None or custom_enabled,
            traffic_price_per_gb_kopeks=2,
            min_traffic_gb=10,
            max_traffic_gb=500,
            device_limit=base,
            max_device_limit=tariff_max,
            device_price_kopeks=100,
            period_prices={'30': 300, '45': 450},
            is_daily=daily,
            daily_price_kopeks=100,
            custom_days_enabled=True,
            price_per_day_kopeks=10,
            allowed_squads=['test-squad'],
        )
        db.add(tariff)
        for sub_id, owner, panel_id in [(10, 1, 9001), (20, 1 if multi else 2, 9002)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    tariff_id=None,
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    subscription_url='https://vpn.example/sub/test',
                    is_trial=trial if sub_id == 10 else False,
                    status='active',
                    end_date=now + timedelta(days=10 if sub_id == 10 else 50),
                    device_limit=old if sub_id == 10 else 7,
                    traffic_used_gb=17,
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        sub = await db.get(Subscription, 10)
        expected_extra = 0 if base == 0 else max(0, expected - base)
        expected_price = (100 if daily else days * 10) + expected_extra * 100 * max(1, round(days / 30))
        expected_price += (custom_traffic or 0) * 2
        saved_cart = None
        if flow in ('auto_cabinet_cart', 'telegram_cabinet_cart'):
            from fastapi import HTTPException

            saved = AsyncMock()
            monkeypatch.setattr(cabinet.user_cart_service, 'save_user_cart', saved)
            user.balance_kopeks = 1
            await db.commit()
            with pytest.raises(HTTPException) as error:
                await cabinet.purchase_tariff(
                    cabinet.TariffPurchaseRequest(
                        tariff_id=1, period_days=days, traffic_gb=custom_traffic, subscription_id=10 if multi else None
                    ),
                    user,
                    db,
                )
            assert error.value.status_code == 402
            saved_cart = saved.await_args.args[1]
            assert saved_cart['traffic_limit_gb'] == (custom_traffic if custom_traffic is not None else 100)
            user.balance_kopeks = 10000
            await db.commit()
        if flow == 'cabinet':
            preview = await cabinet._build_tariff_response(db, tariff, user=user, subscription=sub)
            assert preview['device_limit'] == expected
            assert preview['extra_devices_count'] == expected_extra
            if daily:
                assert preview['daily_price_kopeks'] == expected_price
            if days in [30, 45]:
                period = next(p for p in preview['periods'] if p['days'] == days)
                assert period['price_kopeks'] == expected_price - (custom_traffic or 0) * 2
            response = await cabinet.purchase_tariff(
                cabinet.TariffPurchaseRequest(
                    tariff_id=1, period_days=days, traffic_gb=custom_traffic, subscription_id=10 if multi else None
                ),
                user,
                db,
            )
            assert response['success']
        elif flow in ('auto', 'auto_cabinet_cart'):
            assert await auto._auto_purchase_tariff(
                db, user, saved_cart or {'tariff_id': 1, 'period_days': days, 'subscription_id': 10}
            )
        elif flow == 'auto_daily':
            assert await auto._auto_purchase_daily_tariff(db, user, {'tariff_id': 1, 'subscription_id': 10})
        else:
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
            await state.update_data(target_subscription_id=10, active_subscription_id=10)
            callback = SimpleNamespace(
                data=f'tariff_confirm:1:{days}',
                answer=AsyncMock(),
                bot=SimpleNamespace(send_message=AsyncMock()),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            )
            if flow in ('telegram_cart_daily', 'telegram_cart_period', 'telegram_cabinet_cart'):
                await state.update_data(target_subscription_id=None, active_subscription_id=20)
                await telegram.return_to_saved_tariff_cart(
                    callback,
                    state,
                    user,
                    db,
                    saved_cart
                    or {
                        'cart_mode': 'daily_tariff_purchase' if daily else 'tariff_purchase',
                        'tariff_id': 1,
                        'period_days': days,
                        'subscription_id': 10,
                        'total_price': 100,
                        'daily_price_kopeks': 100,
                    },
                )
                preview = callback.message.edit_text.await_args.args[0]
                assert f'Устройств: {expected}' in preview
                assert telegram.format_price_kopeks(expected_price) in preview
                assert (await state.get_data())['target_subscription_id'] == 10
                callback.data = (
                    callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
                )
                handler = (
                    telegram.confirm_daily_tariff_purchase
                    if daily
                    else (
                        telegram.handle_custom_confirm
                        if callback.data.startswith('custom_confirm:')
                        else telegram.confirm_tariff_purchase
                    )
                )
                await inspect.unwrap(handler)(callback, user, db, state)
            elif flow == 'telegram_daily':
                await telegram._proceed_with_selected_tariff(callback, user, db, state, 1)
                preview = callback.message.edit_text.await_args.args[0]
                assert f'Устройств: {expected}' in preview
                assert telegram.format_price_kopeks(expected_price) in preview
                callback.data = 'daily_tariff_confirm:1'
                await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
                if trial:
                    assert telegram.AdminNotificationService.send_subscription_purchase_notification.await_args.kwargs[
                        'was_trial_conversion'
                    ]
            elif flow == 'telegram_custom':
                await state.update_data(custom_days=days, custom_traffic_gb=100)
                preview = await telegram.format_custom_tariff_preview(
                    tariff,
                    days,
                    100,
                    user.balance_kopeks,
                    db_user=user,
                    db=db,
                    state=state,
                )
                assert f'Итого: {telegram.format_price_kopeks(expected_price)}' in preview
                assert f'Устройств: {telegram.Texts.format_device_limit(expected)}' in preview
                callback.data = 'custom_confirm:1'
                await inspect.unwrap(telegram.handle_custom_confirm)(callback, user, db, state)
                success_text = callback.message.edit_text.await_args.args[0]
                assert f'Устройств: {expected}' in success_text
            else:
                callback.data = f'tariff_period:1:{days}'
                await inspect.unwrap(telegram.select_tariff_period)(callback, user, db, state)
                preview_state = await state.get_data()
                assert preview_state['final_price'] == expected_price
                if multi:
                    assert preview_state['target_subscription_id'] == 10
                callback.data = f'tariff_confirm:1:{days}'
                await inspect.unwrap(telegram.confirm_tariff_purchase)(callback, user, db, state)
        if repeat:
            if flow == 'auto_daily':
                assert not await auto._auto_purchase_daily_tariff(db, user, {'tariff_id': 1, 'subscription_id': 10})
            else:
                await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
        if trial and flow == 'telegram_cart_period':
            call = trial_notice.await_args
            assert call.kwargs.get('was_trial_conversion', call.args[5] if len(call.args) > 5 else None) is True
        await db.refresh(sub)
        await db.refresh(user)
        assert sub.traffic_limit_gb == (custom_traffic if custom_traffic is not None else 100)
        assert sub.tariff_id == 1 and sub.device_limit == expected
        assert sub.remnawave_id == 9001 and sub.subscription_url == 'https://vpn.example/sub/test'
        assert user.balance_kopeks == 10000 - expected_price
        payments = (await db.scalars(select(Transaction))).all()
        assert len(payments) == 1 and payments[0].amount_kopeks == -expected_price
        assert len((await db.scalars(select(Subscription))).all()) == 2
        assert (await db.get(Subscription, 20)).device_limit == 7
        assert (await db.get(Subscription, 20)).end_date == now + timedelta(days=50)
        assert len(panel.patches) == 1 and panel.patches[0]['hwidDeviceLimit'] == expected
        assert (
            panel.patches[0]['trafficLimitBytes'] == (custom_traffic if custom_traffic is not None else 100) * 1024**3
        )


@pytest.mark.parametrize(
    ('base', 'old', 'tariff_max', 'global_max', 'expected'),
    [
        (5, 3, None, 10, 5),
        (1, 5, 3, 10, 3),
        (1, 5, None, 3, 3),
        (0, 3, None, 1, 0),
        (1, 10, 5, 10, 5),
        (2, 10, 5, 10, 5),
    ],
)
async def test_classic_migration_limits(postgres_database, monkeypatch, base, old, tariff_max, global_max, expected):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        'cabinet',
        True,
        base=base,
        old=old,
        tariff_max=tariff_max,
        global_max=global_max,
        expected=expected,
    )


@pytest.mark.parametrize('days', [45, 75])
async def test_classic_migration_non_month_period_price(postgres_database, monkeypatch, days):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database, monkeypatch, 'cabinet', True, days=days
    )


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize(
    ('base', 'old', 'tariff_max', 'expected'),
    [(1, 3, None, 3), (1, 10, 5, 5), (2, 10, 5, 5), (0, 3, None, 0), (5, 3, None, 5)],
)
async def test_classic_custom_telegram_selected_purchase(
    postgres_database,
    monkeypatch,
    multi,
    base,
    old,
    tariff_max,
    expected,
):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        'telegram_custom',
        multi,
        days=75,
        base=base,
        old=old,
        tariff_max=tariff_max,
        expected=expected,
    )


@pytest.mark.parametrize('multi', [False, True])
async def test_cabinet_daily_classic_price_matches_charge(postgres_database, monkeypatch, multi):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database, monkeypatch, 'cabinet', multi, days=1, daily=True
    )


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('flow', ['telegram_daily', 'auto_daily'])
@pytest.mark.parametrize(
    ('base', 'old', 'tariff_max', 'expected'),
    [(1, 3, None, 3), (1, 10, 5, 5), (2, 10, 5, 5), (0, 3, None, 0), (5, 3, None, 5)],
)
async def test_daily_selected_classic_devices(
    postgres_database, monkeypatch, multi, flow, base, old, tariff_max, expected
):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        flow,
        multi,
        days=1,
        daily=True,
        base=base,
        old=old,
        tariff_max=tariff_max,
        expected=expected,
    )


@pytest.mark.parametrize('flow', ['cabinet', 'telegram', 'auto', 'telegram_custom', 'telegram_daily', 'auto_daily'])
@pytest.mark.parametrize('selected', [21, 999])
async def test_invalid_selected_classic_subscription_is_rejected_before_pricing(
    postgres_database, monkeypatch, flow, selected
):
    from fastapi import HTTPException

    from app.services.pricing_engine import pricing_engine

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    pricing = AsyncMock(side_effect=AssertionError('Invalid selected source reached pricing'))
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', pricing)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, balance_kopeks=10000, language='ru'),
                User(id=2, telegram_id=202),
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    device_limit=1,
                    period_prices={'30': 300},
                    is_daily=flow.endswith('daily'),
                    daily_price_kopeks=100,
                ),
            ]
        )
        for sub_id, owner in [(10, 1), (21, 2)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    tariff_id=None,
                    status='active',
                    is_trial=False,
                    device_limit=3,
                    end_date=datetime.now(UTC) + timedelta(days=10),
                    remnawave_short_id=f'test-{sub_id}',
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        if flow == 'cabinet':
            with pytest.raises(HTTPException) as error:
                await cabinet.purchase_tariff(
                    cabinet.TariffPurchaseRequest(tariff_id=1, period_days=30, subscription_id=selected), user, db
                )
            assert error.value.status_code == 404
        elif flow in ('auto', 'auto_daily'):
            auto_handler = auto._auto_purchase_daily_tariff if flow == 'auto_daily' else auto._auto_purchase_tariff
            assert not await auto_handler(db, user, {'tariff_id': 1, 'period_days': 30, 'subscription_id': selected})
        else:
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
            await state.update_data(target_subscription_id=selected)
            callback = SimpleNamespace(
                data='tariff_confirm:1:30', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
            )
            handler = telegram.handle_custom_confirm if flow == 'telegram_custom' else telegram.confirm_tariff_purchase
            if flow == 'telegram_daily':
                callback.data = 'daily_tariff_confirm:1'
                handler = telegram.confirm_daily_tariff_purchase
            await inspect.unwrap(handler)(callback, user, db, state)
            assert callback.answer.await_count == 1
        pricing.assert_not_awaited()
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert (await db.scalars(select(Transaction))).all() == []
        assert (await db.get(Subscription, 10)).device_limit == 3


async def test_divergent_paid_pin_falls_back_to_requested_tariff_before_pricing(postgres_database, monkeypatch):
    from app.services.pricing_engine import pricing_engine

    class PricingBoundary(BaseException):
        pass

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    pricing = AsyncMock(side_effect=PricingBoundary)
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', pricing)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, balance_kopeks=10000, language='ru'))
        for tariff_id in [1, 2]:
            db.add(
                Tariff(
                    id=tariff_id, name=f'Test {tariff_id}', is_active=True, device_limit=1, period_prices={'30': 300}
                )
            )
        for sub_id, tariff_id, devices in [(10, 2, 3), (20, 1, 7)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=1,
                    tariff_id=tariff_id,
                    status='active',
                    is_trial=False,
                    device_limit=devices,
                    end_date=datetime.now(UTC) + timedelta(days=10),
                    remnawave_short_id=f'test-{sub_id}',
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        await state.update_data(target_subscription_id=10)
        callback = SimpleNamespace(
            data='tariff_confirm:1:30', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
        )
        with pytest.raises(PricingBoundary):
            await inspect.unwrap(telegram.confirm_tariff_purchase)(callback, user, db, state)
        assert pricing.await_args.kwargs['device_limit'] == 7
        assert (await db.get(Subscription, 10)).tariff_id == 2
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert (await db.scalars(select(Transaction))).all() == []


async def test_custom_fallback_prefers_active_tariff_over_later_disabled_record(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101))
        db.add(Tariff(id=1, name='Test', device_limit=1, is_active=True))
        now = datetime.now(UTC)
        for sub_id, status, days in [(10, 'active', 5), (20, 'disabled', 50)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=1,
                    tariff_id=1,
                    is_trial=False,
                    status=status,
                    end_date=now + timedelta(days=days),
                    device_limit=3,
                    remnawave_short_id=f'test-{sub_id}',
                )
            )
        await db.commit()
        selected = await telegram._resolve_purchase_subscription(
            db,
            await get_user_by_id(db, 1),
            await db.get(Tariff, 1),
        )
        assert selected.id == 10


async def test_daily_insufficient_balance_cart_replays_selected_classic(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    saved_cart = AsyncMock()
    monkeypatch.setattr(telegram.user_cart_service, 'save_user_cart', saved_cart)
    now = datetime.now(UTC)
    panel = Panel(now)

    @asynccontextmanager
    async def client(self):
        yield panel

    monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=150, remnawave_id=9002))
        db.add(
            Tariff(
                id=1,
                name='Test',
                is_active=True,
                is_daily=True,
                daily_price_kopeks=100,
                device_limit=1,
                device_price_kopeks=100,
                traffic_limit_gb=100,
                allowed_squads=['test-squad'],
            )
        )
        for sub_id, panel_id, cap in [(10, 9001, 3), (20, 9002, 7)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=1,
                    tariff_id=None,
                    is_trial=False,
                    status='active',
                    remnawave_id=panel_id,
                    remnawave_short_id=f'test-{sub_id}',
                    subscription_url='https://vpn.example/sub/test',
                    device_limit=cap,
                    end_date=now + timedelta(days=10),
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        await state.update_data(active_subscription_id=10)
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        await telegram._proceed_with_selected_tariff(callback, user, db, state, 1)
        saved_cart.assert_awaited_once()
        cart = saved_cart.await_args.args[1]
        assert cart['subscription_id'] == 10 and cart['device_limit'] == 3
        assert cart['total_price'] == cart['daily_price_kopeks'] == 300
        assert cart['missing_amount'] == 150
        assert (await db.scalars(select(Transaction))).all() == []
        assert user.balance_kopeks == 150
        user.balance_kopeks = 10000
        await db.commit()
        assert await auto._auto_purchase_daily_tariff(db, user, cart)
        await db.refresh(user)
        assert user.balance_kopeks == 9700
        selected = await db.get(Subscription, 10)
        assert selected.tariff_id == 1 and selected.device_limit == 3 and selected.remnawave_id == 9001
        assert (await db.get(Subscription, 20)).device_limit == 7
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -300
        assert panel.patches[-1]['hwidDeviceLimit'] == 3


@pytest.mark.parametrize('flow', ['telegram_daily', 'auto_daily'])
@pytest.mark.parametrize('multi', [False, True])
async def test_repeated_daily_purchase_does_not_charge_twice(postgres_database, monkeypatch, flow, multi):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        flow,
        multi,
        days=1,
        daily=True,
        repeat=True,
    )


@pytest.mark.parametrize('multi', [False, True])
async def test_daily_selected_trial_keeps_conversion_marker(postgres_database, monkeypatch, multi):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        'telegram_daily',
        multi,
        days=1,
        daily=True,
        trial=True,
        expected=1,
    )


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize(
    ('flow', 'days', 'daily'),
    [
        ('telegram_cart_daily', 1, True),
        ('telegram_cart_period', 30, False),
        ('telegram_cart_period', 75, False),
    ],
)
async def test_saved_cart_return_keeps_selected_classic_and_requotes(
    postgres_database, monkeypatch, multi, flow, days, daily
):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        flow,
        multi,
        days=days,
        daily=daily,
    )


@pytest.mark.parametrize('flow', ['cabinet', 'auto_cabinet_cart', 'telegram_cabinet_cart'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('days', [30, 75])
async def test_custom_traffic_survives_saved_checkout(postgres_database, monkeypatch, flow, multi, days):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        flow,
        multi,
        days=days,
        custom_traffic=250,
    )


@pytest.mark.parametrize('flow', ['auto_cabinet_cart', 'telegram_cabinet_cart'])
@pytest.mark.parametrize('custom_traffic', [None, 100])
async def test_cart_distinguishes_default_from_explicit_base_traffic(
    postgres_database, monkeypatch, flow, custom_traffic
):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        flow,
        True,
        days=75,
        custom_traffic=custom_traffic,
        custom_enabled=True,
    )


@pytest.mark.parametrize('days', [30, 75])
async def test_restored_trial_keeps_conversion_notice(postgres_database, monkeypatch, days):
    await test_selected_classic_devices_are_priced_and_preserved(
        postgres_database,
        monkeypatch,
        'telegram_cart_period',
        True,
        days=days,
        trial=True,
        expected=1,
    )
