"""#2879: multi renewal exposes switching and retains the selected source."""

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
from app.handlers.subscription import tariff_purchase as tp
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('flow', ['renew', 'regular', 'instant', 'explicit'])
@pytest.mark.parametrize('selected', [11, 12])
async def test_multi_renew_reaches_switch_for_selected_subscription(
    postgres_database, monkeypatch, flow, selected, reason='active'
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', reason != 'single')
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', reason != 'disabled')
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', reason != 'disabled')
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=101, language='ru', status='active', balance_kopeks=10000))
        for tariff_id in [1, 2, 3]:
            db.add(
                Tariff(
                    id=tariff_id,
                    name=f'Tariff {tariff_id}',
                    is_active=True,
                    traffic_limit_gb=tariff_id * 100,
                    device_limit=2,
                    period_prices={'30': tariff_id * 1000},
                )
            )
        for sub_id, tariff_id in [(11, 1), (12, 2)]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=1,
                    tariff_id=tariff_id,
                    remnawave_short_id=f'test-{sub_id}',
                    status='active',
                    is_trial=reason == 'trial' and sub_id == selected,
                    end_date=now + timedelta(days=-1 if reason == 'expired' and sub_id == selected else 10),
                    device_limit=2,
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        await state.update_data(active_subscription_id=12 if selected == 11 else 11, stale_checkout='discard')
        callback = SimpleNamespace(
            data=f'se:{selected}', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
        )
        if flow == 'renew':
            await tp.show_tariff_extend(callback, user, db)
        else:
            if flow != 'explicit':
                await state.update_data(active_subscription_id=selected)
            callback.data = (
                f'tariff_switch_for:{selected}'
                if flow == 'explicit'
                else 'tariff_switch'
                if flow == 'regular'
                else 'instant_switch'
            )
            handler = tp.show_tariff_switch_list if flow == 'regular' else tp.show_instant_switch_list
            await handler(callback, user, db, state)
        assert callback.message.edit_text.await_count == 1
        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        if flow == 'renew':
            assert (f'tariff_switch_for:{selected}' in callbacks) is (reason == 'active')
        else:
            assert any(value and value.endswith(':3') for value in callbacks)
            data = await state.get_data()
            assert data['active_subscription_id'] == selected
            assert 'stale_checkout' not in data
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert (await db.scalars(select(Transaction))).all() == []


@pytest.mark.parametrize(('selected', 'multi'), [('21', True), ('999', True), ('bad', True), ('11', False)])
async def test_explicit_switch_entry_rejects_foreign_missing_or_invalid_source(
    postgres_database, monkeypatch, selected, multi
):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, language='ru'),
                User(id=2, telegram_id=202, language='ru'),
                Subscription(
                    id=11,
                    user_id=1,
                    status='active',
                    is_trial=False,
                    remnawave_short_id='owned',
                    end_date=datetime.now(UTC) + timedelta(days=10),
                ),
                Subscription(
                    id=21,
                    user_id=2,
                    status='active',
                    is_trial=False,
                    remnawave_short_id='foreign',
                    end_date=datetime.now(UTC) + timedelta(days=10),
                ),
            ]
        )
        await db.commit()
        user = await get_user_by_id(db, 1)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        await state.update_data(active_subscription_id=11)
        callback = SimpleNamespace(
            data=f'tariff_switch_for:{selected}', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
        )
        await tp.show_instant_switch_list(callback, user, db, state)
        assert callback.answer.await_count == 1
        assert callback.message.edit_text.await_count == 0


@pytest.mark.parametrize('reason', ['expired', 'trial', 'single', 'disabled'])
async def test_renew_switch_entry_respects_existing_policy(postgres_database, monkeypatch, reason):
    await test_multi_renew_reaches_switch_for_selected_subscription(postgres_database, monkeypatch, 'renew', 11, reason)
