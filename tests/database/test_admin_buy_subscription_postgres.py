import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import transaction as transaction_crud, user as user_crud
from app.database.models import Subscription, Tariff, TrafficPurchase, Transaction, User
from app.handlers.admin import users
from app.services.remnawave_service import RemnaWaveService
from tests.database.test_multi_tariff_switch_postgres import switch_data


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    'fault',
    [None, 'ledger', 'commit', 'cancel', 'owner_changed', 'carry', 'extras', 'price_up', 'price_down', 'legacy'],
)
async def test_selected_tariff_trial_purchase_is_atomic(postgres_database, monkeypatch, fault):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', fault == 'carry')
        monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
        chosen = await db.get(Subscription, 12)
        chosen.is_trial = True
        (await db.get(Tariff, 2)).device_price_kopeks = 100
        chosen.traffic_used_gb = 17
        # Extra devices have a price and must survive the paid conversion.
        chosen.device_limit = 4
        if fault == 'extras':
            chosen.traffic_limit_gb = 225
            chosen.purchased_traffic_gb = 25
            db.add(
                TrafficPurchase(subscription_id=12, traffic_gb=25, expires_at=datetime.now(UTC) + timedelta(days=10))
            )
        await db.commit()
        quote_user = await user_crud.lock_user_for_pricing(db, user.id)
        quoted_price = await users._calculate_subscription_period_price(db, quote_user, chosen, 30)
        await db.commit()
        if fault in ('price_up', 'price_down'):
            (await db.get(Tariff, 2)).period_prices = {'30': 3000 if fault == 'price_up' else 1000}
            await db.commit()
        before_user = (await db.execute(select(User.__table__).where(User.id == 1))).mappings().one()
        before_chosen = (await db.execute(select(Subscription.__table__).where(Subscription.id == 12))).mappings().one()
        before_sibling = (
            (await db.execute(select(Subscription.__table__).where(Subscription.id == 11))).mappings().one()
        )
        monkeypatch.setattr(
            users.UserService, 'get_user_profile', AsyncMock(return_value={'user': user, 'subscription': chosen})
        )
        # Stop external panel I/O after the financial transaction, without mocking billing.
        monkeypatch.setattr(
            RemnaWaveService, 'get_api_client', lambda self: (_ for _ in ()).throw(RuntimeError('panel unavailable'))
        )
        emitted = AsyncMock()
        monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', emitted)
        if fault in ('ledger', 'cancel'):

            async def fail_ledger(*args, **kwargs):
                if fault == 'cancel':
                    raise asyncio.CancelledError
                raise RuntimeError('ledger insertion failed')

            monkeypatch.setattr(transaction_crud, 'create_transaction', fail_ledger)
        elif fault == 'commit':
            original_commit = db.commit

            async def fail_subscription_commit():
                if any(isinstance(item, Subscription) for item in db.dirty):
                    raise RuntimeError('financial commit failed')
                # The atomic path flushes before final commit, so also detect its ledger.
                if (await db.scalar(select(Transaction.id).limit(1))) is not None:
                    raise RuntimeError('financial commit failed')
                await original_commit()

            monkeypatch.setattr(db, 'commit', fail_subscription_commit)
        elif fault == 'owner_changed':
            original_lock = user_crud.lock_user_for_pricing

            async def changed_owner(session, user_id):
                async with AsyncSession(bind=db.bind, expire_on_commit=False) as other:
                    await other.execute(update(Subscription).where(Subscription.id == 12).values(user_id=2))
                    await other.commit()
                return await original_lock(session, user_id)

            monkeypatch.setattr(user_crud, 'lock_user_for_pricing', changed_owner)
        callback = SimpleNamespace(
            data=(
                'admin_buy_sub_execute_1_30_s12'
                if fault == 'legacy'
                else f'admin_buy_sub_execute_1_30_{quoted_price}_s12'
            ),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
            bot=None,
        )
        if fault == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await inspect.unwrap(users.admin_buy_subscription_execute)(callback, user, db)
            assert not db.in_transaction(), 'Cancellation must release locks and roll back immediately'
        else:
            await inspect.unwrap(users.admin_buy_subscription_execute)(callback, user, db)
        await db.rollback()
        after_user = (await db.execute(select(User.__table__).where(User.id == 1))).mappings().one()
        after_chosen = (await db.execute(select(Subscription.__table__).where(Subscription.id == 12))).mappings().one()
        assert (
            await db.execute(select(Subscription.__table__).where(Subscription.id == 11))
        ).mappings().one() == before_sibling
        ledger = list(await db.scalars(select(Transaction)))
        if fault in ('ledger', 'commit', 'cancel', 'owner_changed', 'price_up', 'price_down'):
            assert after_user == before_user
            expected = dict(before_chosen)
            if fault == 'owner_changed':
                expected['user_id'] = 2
                expected['updated_at'] = after_chosen['updated_at']
            assert dict(after_chosen) == expected
            assert ledger == []
            emitted.assert_not_awaited()
            if fault in ('price_up', 'price_down'):
                assert 'Стоимость изменилась' in callback.answer.await_args.args[0]
        else:
            assert (
                after_chosen['traffic_limit_gb']
                == before_chosen['traffic_limit_gb']
                == (225 if fault == 'extras' else 200)
            )
            if fault == 'extras':
                assert after_chosen['purchased_traffic_gb'] == 25
                assert (await db.scalar(select(TrafficPurchase.traffic_gb))) == 25
            assert after_chosen['device_limit'] == 4
            assert not after_chosen['is_trial']
            assert after_chosen['end_date'] > before_chosen['end_date']
            expected_end = (before_chosen['end_date'] if fault == 'carry' else datetime.now(UTC)) + timedelta(days=30)
            assert abs((after_chosen['end_date'] - expected_end).total_seconds()) < 5
            assert len(ledger) == 1
            assert after_user['balance_kopeks'] == before_user['balance_kopeks'] + ledger[0].amount_kopeks
            assert ledger[0].amount_kopeks < -2000
            emitted.assert_awaited_once()
