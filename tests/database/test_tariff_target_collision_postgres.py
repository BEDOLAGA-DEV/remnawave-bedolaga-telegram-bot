"""Owned alive tariff rows must reject a different selected source before quoting."""

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase as cabinet
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.pricing_engine import pricing_engine
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    'flow,daily', [('auto', False), ('auto', True), ('telegram', True), ('cabinet', False), ('cabinet', True)]
)
@pytest.mark.parametrize('target_status', ['active', 'trial', 'limited'])
@pytest.mark.parametrize('source_trial', [False, True])
async def test_selected_classic_rejects_owned_alive_target_before_quote(
    postgres_database, monkeypatch, flow, daily, target_status, source_trial, target_owner=1, conflict=True
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    quote = AsyncMock(side_effect=AssertionError('conflict reached pricing'))
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', quote)
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all([User(id=1, telegram_id=101, balance_kopeks=10000), User(id=2, telegram_id=202)])
        db.add(
            Tariff(
                id=1,
                name='Target',
                is_active=True,
                period_prices={'30': 300},
                device_limit=1,
                is_daily=daily,
                daily_price_kopeks=100,
            )
        )
        db.add_all(
            [
                Subscription(
                    id=10,
                    user_id=1,
                    tariff_id=None,
                    status='active',
                    device_limit=10,
                    remnawave_short_id='source',
                    is_trial=source_trial,
                    end_date=now + timedelta(days=10),
                ),
                Subscription(
                    id=20,
                    user_id=target_owner,
                    tariff_id=1,
                    status=target_status,
                    device_limit=5,
                    remnawave_short_id='target',
                    is_trial=False,
                    end_date=now + timedelta(days=20),
                ),
            ]
        )
        await db.commit()
        user = await get_user_by_id(db, 1)
        if flow == 'auto':
            purchase = auto._auto_purchase_daily_tariff if daily else auto._auto_purchase_tariff
            cart = {'tariff_id': 1, 'period_days': 30, 'subscription_id': 10}
            if conflict:
                assert not await purchase(db, user, cart)
            else:
                # Stop at the first quote: only the admission rule is under test.
                with pytest.raises(AssertionError, match='conflict reached pricing'):
                    await purchase(db, user, cart)
        elif flow == 'cabinet':
            with pytest.raises(HTTPException) as error:
                await cabinet.purchase_tariff(
                    cabinet.TariffPurchaseRequest(tariff_id=1, period_days=1 if daily else 30, subscription_id=10),
                    user,
                    db,
                )
            assert error.value.status_code == 409
        else:
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
            await state.update_data(target_subscription_id=10)
            callback = SimpleNamespace(
                data='daily_tariff_confirm:1', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
            )
            await inspect.unwrap(telegram.confirm_daily_tariff_purchase)(callback, user, db, state)
            assert callback.answer.await_args.kwargs.get('show_alert') is True
        if conflict:
            quote.assert_not_awaited()
        else:
            quote.assert_awaited_once()
        await db.commit()
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        source = await db.get(Subscription, 10)
        target = await db.get(Subscription, 20)
        assert (source.tariff_id, source.device_limit, source.end_date) == (None, 10, now + timedelta(days=10))
        assert (target.tariff_id, target.device_limit, target.end_date, target.status) == (
            1,
            5,
            now + timedelta(days=20),
            target_status,
        )
        assert list(await db.scalars(select(Transaction))) == []


@pytest.mark.parametrize('daily', [False, True])
@pytest.mark.parametrize('target_status,target_owner', [('expired', 1), ('disabled', 1), ('active', 2)])
async def test_inactive_or_foreign_target_does_not_block_selected_classic(
    postgres_database, monkeypatch, daily, target_status, target_owner
):
    await test_selected_classic_rejects_owned_alive_target_before_quote(
        postgres_database,
        monkeypatch,
        'auto',
        daily,
        target_status,
        source_trial=False,
        target_owner=target_owner,
        conflict=False,
    )
