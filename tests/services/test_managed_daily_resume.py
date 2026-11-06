"""Daily resume commits its paid grant intent with money on every entry point."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database.models import Base, Subscription, TrafficAccountingState, TrafficBaseGrantIntent, Transaction, User
from app.services.traffic_accounting import AccountingConflict
from tests.fixtures.sqlite_memory import memory_session
from tests.handlers import test_daily_bot_resume_traffic_reset as bot_tests
from tests.services import test_daily_resume_traffic_reset as api_tests


@pytest.mark.parametrize('endpoint', ['cabinet', 'miniapp', 'telegram'])
@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('reset', [False, True])
async def test_managed_daily_resume_atomic(monkeypatch, endpoint, failure, reset):
    module = bot_tests if endpoint == 'telegram' else api_tests
    original = module._rows
    accounting_states = []

    def rows(*args, **kwargs):
        result = original(*args, **kwargs)
        sub = next(row for row in result if isinstance(row, Subscription))
        sub.traffic_accounting_mode = 'managed'
        accounting_states.append(
            TrafficAccountingState(
                subscription_id=sub.id,
                panel_identity='masked-1',
                lifetime_bytes=90 * 1024**3,
                used_bytes=90 * 1024**3,
                phase='ready',
                business_strategy='NO_RESET',
                calendar_timezone='UTC',
                calendar_anchor=datetime.now(UTC),
            )
        )
        return result

    monkeypatch.setattr(module, '_rows', rows)
    from app.services.traffic_purchase_service import lock_daily_accounting_subscription

    async def seed_then_lock(db, subscription, user_id):
        if accounting_states:
            db.add(accounting_states.pop())
            await db.commit()
        return await lock_daily_accounting_subscription(db, subscription, user_id)

    monkeypatch.setattr('app.services.traffic_purchase_service.lock_daily_accounting_subscription', seed_then_lock)
    if failure and reset:
        monkeypatch.setattr(
            'app.services.traffic_base_grant_service.stage_paid_base_grant',
            AsyncMock(side_effect=AccountingConflict('injected intent failure')),
        )
    invoke = {
        'cabinet': api_tests._resume_from_cabinet,
        'miniapp': api_tests._resume_from_miniapp,
        'telegram': bot_tests._resume_from_bot,
    }[endpoint]
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        if failure and reset:
            with pytest.raises(AccountingConflict):
                await invoke(db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=reset)
            await db.rollback()
            assert (await db.get(User, 1)).balance_kopeks == 100_000
            assert (await db.get(Subscription, 10)).status == 'disabled'
            assert list(await db.scalars(select(Transaction))) == []
        else:
            result = await invoke(db, monkeypatch, traffic_reset_mode='NO_RESET', reset_on_payment=reset)
            panel = result[0]
            assert (await db.get(User, 1)).balance_kopeks == 99_000
            assert (await db.get(Subscription, 10)).status == 'active'
            assert (await db.get(TrafficAccountingState, 10)).activation_requested is True
            transactions = list(await db.scalars(select(Transaction)))
            assert len(transactions) == 1
            intents = list(await db.scalars(select(TrafficBaseGrantIntent)))
            assert len(intents) == int(reset)
            if reset:
                assert intents[0].operation_key == f'daily-resume:{transactions[0].id}'
                assert intents[0].state == 'pending'
            assert panel.calls and all(not call.get('reset_traffic') for call in panel.calls)


@pytest.mark.parametrize('reset', [False, True])
async def test_paid_daily_resume_reopens_disabled_panel_without_regranting_by_default(monkeypatch, reset):
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.traffic_accounting_delivery import deliver_account_projection
    from app.services.traffic_accounting_service import lock_accounting_subscription
    from app.services.traffic_accounting_store import load_account
    from app.services.traffic_purchase_service import stage_daily_resume_grant
    from tests.services.panel_sync.test_managed_write_masks import prepare
    from tests.services.test_traffic_accounting_service import NOW
    from tests.services.test_traffic_accounting_store import database

    monkeypatch.setattr('app.services.traffic_reset_policy.should_reset_traffic_on_daily_charge', lambda *_: reset)
    monkeypatch.setattr('app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *_: None)
    async with database(monkeypatch) as db:
        api, _ = await prepare(db)
        api.user.status = 'DISABLED'
        sub, _ = await lock_accounting_subscription(db, 1)
        sub.status = 'active'
        sub.is_daily_paused = False
        sub.last_daily_charge_at = NOW
        # The entry-point tests above prove the debit; here exercise actual
        # durable projection against an administratively DISABLED-shaped panel.
        await stage_daily_resume_grant(db, sub, SimpleNamespace(), SimpleNamespace(id=12))
        await db.commit()
        await deliver_account_projection(async_sessionmaker(db.bind, expire_on_commit=False), api, 1, now=NOW)
        assert api.writes[-1]['status'].value == 'ACTIVE'
        assert (await db.get(Subscription, 1)).status == 'active'
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == (1024**3 if reset else 70)
