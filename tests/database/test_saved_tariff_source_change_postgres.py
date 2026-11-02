"""Saved purchases must not reinterpret a source that changed to another tariff."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.pricing_engine import pricing_engine
from tests.fixtures.postgres_db import postgres_engine


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('entry', ['auto_daily', 'restore_daily', 'restore_period'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('source_status', ['active', 'expired'])
@pytest.mark.parametrize('target_exists', [False, True])
async def test_saved_source_change_respects_multi_selection_and_single_switch_policy(
    postgres_database, monkeypatch, entry, multi, source_status, target_exists
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    quote = AsyncMock(side_effect=AssertionError('stale source reached pricing'))
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', quote)
    now = datetime.now(UTC)
    daily = entry != 'restore_period'
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            db.add(User(id=1, telegram_id=101, balance_kopeks=10000))
            db.add_all(
                [
                    Tariff(
                        id=1,
                        name='Requested',
                        is_active=True,
                        is_daily=daily,
                        daily_price_kopeks=100,
                        device_limit=1,
                        period_prices={'30': 300},
                    ),
                    Tariff(id=2, name='Changed', is_active=True, device_limit=7),
                ]
            )
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=2,
                    status=source_status,
                    is_trial=False,
                    remnawave_short_id='source',
                    device_limit=7,
                    end_date=now + timedelta(days=10),
                )
            )
            if target_exists:
                db.add(
                    Subscription(
                        id=20,
                        user_id=1,
                        tariff_id=1,
                        status='active',
                        is_trial=False,
                        remnawave_short_id='target',
                        device_limit=5,
                        end_date=now + timedelta(days=20),
                    )
                )
            await db.commit()
        cart = {
            'cart_mode': 'daily_tariff_purchase' if daily else 'tariff_purchase',
            'tariff_id': 1,
            'period_days': 1 if daily else 30,
            'subscription_id': 10,
        }
        original_cart = deepcopy(cart)
        async with maker() as db:
            user = await get_user_by_id(db, 1)
            # Single mode already supports switching the primary subscription.
            # Preserve that contract; multi mode pins the saved source identity.
            allowed_quote = not multi and (entry != 'auto_daily' or not target_exists)
            if entry == 'auto_daily':
                if allowed_quote:
                    with pytest.raises(AssertionError, match='stale source reached pricing'):
                        await auto._auto_purchase_daily_tariff(db, user, cart)
                else:
                    assert not await auto._auto_purchase_daily_tariff(db, user, cart)
            else:
                state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
                await state.update_data(active_subscription_id=77, target_subscription_id=77)
                original_state = await state.get_data()
                callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
                if allowed_quote:
                    with pytest.raises(AssertionError, match='stale source reached pricing'):
                        await telegram.return_to_saved_tariff_cart(callback, state, user, db, cart)
                else:
                    await telegram.return_to_saved_tariff_cart(callback, state, user, db, cart)
                    assert callback.answer.await_args.kwargs.get('show_alert') is True
                callback.message.edit_text.assert_not_awaited()
                assert await state.get_data() == original_state
            if allowed_quote:
                quote.assert_awaited_once()
            else:
                quote.assert_not_awaited()
            assert cart == original_cart
            await db.commit()
        async with maker() as observer:
            assert (await observer.get(User, 1)).balance_kopeks == 10000
            source = await observer.get(Subscription, 10)
            assert (source.tariff_id, source.device_limit, source.status, source.end_date) == (
                2,
                7,
                source_status,
                now + timedelta(days=10),
            )
            if target_exists:
                target = await observer.get(Subscription, 20)
                assert (target.tariff_id, target.device_limit, target.end_date) == (1, 5, now + timedelta(days=20))
            assert list(await observer.scalars(select(Transaction))) == []
