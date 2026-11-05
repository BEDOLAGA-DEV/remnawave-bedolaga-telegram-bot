"""Behavioral coverage for binding a Telegram purchase to its owned source row.

This replaces source-shape assertions: pricing/fulfillment can be shared without
losing the selected subscription or allowing a stale callback to debit a new one.
"""

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

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


@asynccontextmanager
async def purchase_context(monkeypatch, *, source=True):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'MAX_ACTIVE_SUBSCRIPTIONS', 1)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru', balance_kopeks=10000),
                User(id=2, telegram_id=202),
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    device_limit=1,
                    device_price_kopeks=100,
                    traffic_limit_gb=100,
                    period_prices={'30': 300},
                    allowed_squads=['test-squad'],
                    custom_days_enabled=True,
                    price_per_day_kopeks=10,
                    min_days=1,
                    max_days=365,
                ),
                Tariff(id=2, name='Neighbor', is_active=True, device_limit=7),
            ]
        )
        db.add(
            Subscription(
                id=20,
                remnawave_short_id='test-20',
                user_id=1,
                tariff_id=2,
                is_trial=False,
                status='active',
                device_limit=7,
                end_date=now + timedelta(days=50),
            )
        )
        db.add(
            Subscription(
                id=21, user_id=2, is_trial=False, status='active', device_limit=5, end_date=now + timedelta(days=50)
            )
        )
        if source:
            db.add(
                Subscription(
                    id=10,
                    remnawave_short_id='test-10',
                    user_id=1,
                    tariff_id=None,
                    is_trial=False,
                    status='expired',
                    device_limit=3,
                    end_date=now - timedelta(days=1),
                )
            )
        await db.commit()
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        if source:
            await state.update_data(target_subscription_id=10, custom_days=75, custom_traffic_gb=100)
        callback = SimpleNamespace(
            data='tariff_period:1:30', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
        )
        yield db, await get_user_by_id(db, 1), state, callback


def confirmation(callback):
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    return next(
        b.callback_data
        for row in keyboard.inline_keyboard
        for b in row
        if b.callback_data and b.callback_data.startswith(('tariff_confirm:', 'custom_confirm:'))
    )


@pytest.mark.parametrize('custom', [False, True])
async def test_purchase_keeps_preview_source_after_status_and_current_selection_change(monkeypatch, custom):
    async with purchase_context(monkeypatch) as (db, user, state, callback):
        if custom:
            callback.data = 'custom_days:1:0'
        preview = telegram.handle_custom_days_change if custom else telegram.select_tariff_period
        await inspect.unwrap(preview)(callback, user, db, state)
        bound = (await state.get_data())['restored_tariff_checkout']['cart']
        assert bound['subscription_id'] == 10
        selected = await db.get(Subscription, 10)
        selected.status = 'disabled'
        await db.commit()
        await state.update_data(target_subscription_id=20)
        callback.data = confirmation(callback)
        handler = telegram.handle_custom_confirm if custom else telegram.confirm_tariff_purchase
        await inspect.unwrap(handler)(callback, user, db, state)
        await db.refresh(user)
        await db.refresh(selected)
        # 75 days: base 750 + two devices at 100/month * 75/30 = 1250.
        assert user.balance_kopeks == 10000 - (1250 if custom else 500)
        assert selected.tariff_id == 1 and selected.device_limit == 3 and selected.status == 'active'
        assert (await db.get(Subscription, 20)).device_limit == 7
        assert (await db.get(Subscription, 20)).tariff_id == 2
        assert len((await db.scalars(select(Transaction))).all()) == 1


@pytest.mark.parametrize('selected', [21, 999])
async def test_foreign_or_missing_preview_source_never_reaches_pricing(monkeypatch, selected):
    from app.services.pricing_engine import pricing_engine

    async with purchase_context(monkeypatch) as (db, user, state, callback):
        await state.update_data(target_subscription_id=selected)
        pricing = AsyncMock(side_effect=AssertionError('Invalid source reached pricing'))
        monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', pricing)
        await inspect.unwrap(telegram.select_tariff_period)(callback, user, db, state)
        pricing.assert_not_awaited()
        assert callback.answer.await_args.kwargs['show_alert']
        assert user.balance_kopeks == 10000
        assert not (await db.scalars(select(Transaction))).all()


async def test_fixed_purchase_preserves_max_active_subscriptions_before_debit(monkeypatch):
    async with purchase_context(monkeypatch, source=False) as (db, user, state, callback):
        await inspect.unwrap(telegram.select_tariff_period)(callback, user, db, state)
        callback.data = confirmation(callback)
        await inspect.unwrap(telegram.confirm_tariff_purchase)(callback, user, db, state)
        assert 'Максимум подписок' in callback.answer.await_args.args[0]
        assert user.balance_kopeks == 10000
        assert not (await db.scalars(select(Transaction))).all()


@pytest.mark.parametrize('custom', [False, True])
async def test_unbound_legacy_confirmation_requires_reselection(monkeypatch, custom):
    async with purchase_context(monkeypatch) as (db, user, state, callback):
        callback.data = 'custom_confirm:1' if custom else 'tariff_confirm:1:30'
        handler = telegram.handle_custom_confirm if custom else telegram.confirm_tariff_purchase
        await inspect.unwrap(handler)(callback, user, db, state)
        assert 'устарело' in callback.answer.await_args.args[0]
        assert user.balance_kopeks == 10000
        assert not (await db.scalars(select(Transaction))).all()


@pytest.mark.parametrize('entry', ['select', 'days', 'traffic', 'period_traffic'])
async def test_custom_preview_buttons_bind_options_and_reject_previous_selection(monkeypatch, entry):
    async with purchase_context(monkeypatch) as (db, user, state, callback):
        tariff = await db.get(Tariff, 1)
        tariff.custom_traffic_enabled = True
        tariff.min_traffic_gb = 0
        tariff.max_traffic_gb = 1000
        tariff.traffic_price_per_gb_kopeks = 1
        await db.commit()
        callback.data, handler = {
            'select': ('tariff:1', telegram.select_tariff),
            'days': ('custom_days:1:0', telegram.handle_custom_days_change),
            'traffic': ('custom_traffic:1:0', telegram.handle_custom_traffic_change),
            'period_traffic': ('tariff_period_traffic:1:30', telegram.select_tariff_period_with_traffic),
        }[entry]
        await inspect.unwrap(handler)(callback, user, db, state)
        old_confirmation = confirmation(callback)
        old_cart = (await state.get_data())['restored_tariff_checkout']['cart']
        callback.data = 'custom_traffic:1:1'
        await inspect.unwrap(telegram.handle_custom_traffic_change)(callback, user, db, state)
        new_confirmation = confirmation(callback)
        new_cart = (await state.get_data())['restored_tariff_checkout']['cart']
        assert old_confirmation != new_confirmation and len(new_confirmation.encode()) <= 64
        assert new_cart['custom_traffic_gb'] == old_cart['custom_traffic_gb'] + 1
        callback.data = old_confirmation
        await inspect.unwrap(telegram.handle_custom_confirm)(callback, user, db, state)
        assert user.balance_kopeks == 10000 and not (await db.scalars(select(Transaction))).all()
        callback.data = new_confirmation
        await inspect.unwrap(telegram.handle_custom_confirm)(callback, user, db, state)
        await db.refresh(user)
        source = await db.get(Subscription, 10)
        await db.refresh(source)
        assert source.traffic_limit_gb == new_cart['custom_traffic_gb']
        assert len((await db.scalars(select(Transaction))).all()) == 1
        # A change after completion also remains usable; the old receipt cannot debit it.
        callback.data = 'custom_traffic:1:1'
        await inspect.unwrap(telegram.handle_custom_traffic_change)(callback, user, db, state)
        assert confirmation(callback) != new_confirmation
