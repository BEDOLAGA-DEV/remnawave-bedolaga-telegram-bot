"""The registered classic quick-renewal handler must convert atomically."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Transaction
from app.handlers.subscription import purchase
from tests.database.test_quick_tariff_renewal_postgres import checkout


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('trial', [False, True])
@pytest.mark.parametrize('fail', [False, True, 'cancel'])
async def test_classic_quick_trial_conversion(postgres_database, monkeypatch, fail, trial):
    async with checkout(postgres_database, monkeypatch) as c:
        monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
        monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_ENABLED', True)
        monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
        c.sub.tariff_id = None
        c.sub.is_trial = trial
        c.sub.traffic_limit_gb = 10
        c.sub.autopay_enabled = False
        await c.db.commit()
        monkeypatch.setattr(purchase, '_calculate_simple_subscription_price', AsyncMock(return_value=(1000, {})))
        panel = SimpleNamespace(update_remnawave_user=AsyncMock())
        monkeypatch.setattr('app.services.subscription_service.SubscriptionService', lambda: panel)
        if fail:
            monkeypatch.setattr(
                'app.database.crud.transaction.create_transaction',
                AsyncMock(side_effect=asyncio.CancelledError() if fail == 'cancel' else RuntimeError('ledger failed')),
            )
        try:
            await purchase._extend_existing_subscription(
                c.callback, c.user, c.db, c.sub, 30, 2 if trial else 5, 100, 'paid-squad'
            )
        except asyncio.CancelledError:
            assert fail == 'cancel'
            assert not c.db.in_transaction()
        except RuntimeError:
            if not fail:
                raise
        await c.db.rollback()
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == (400000 if fail else 399000)
        assert c.sub.is_trial is bool(trial and fail)
        if fail:
            assert c.sub.end_date == c.end
            panel.update_remnawave_user.assert_not_awaited()
            assert list(await c.db.scalars(select(Transaction))) == []
        else:
            assert c.sub.autopay_enabled is trial
            assert c.sub.traffic_used_gb == (0 if trial else 17)
            assert c.sub.traffic_limit_gb == 100
            assert c.sub.device_limit == (2 if trial else 5)
            assert c.sub.connected_squads == (['paid-squad'] if trial else ['test-squad', 'paid-squad'])
            expected_end = (c.now if trial else c.end) + timedelta(days=30)
            assert abs((c.sub.end_date - expected_end).total_seconds()) < 10
            assert panel.update_remnawave_user.await_args.kwargs['reset_traffic'] is trial


@pytest.mark.parametrize('fail', [False, True, 'cancel'])
async def test_trial_daily_switch_is_atomic_and_resets_usage(postgres_database, monkeypatch, fail):
    import inspect

    from app.database.models import Tariff
    from app.handlers.subscription import tariff_purchase as telegram

    async with checkout(postgres_database, monkeypatch) as c:
        monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_ENABLED', True)
        monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', False)
        c.sub.tariff_id = None
        c.sub.is_trial = True
        c.sub.autopay_enabled = False
        c.db.add(
            Tariff(
                id=2,
                name='Daily',
                is_active=True,
                is_daily=True,
                daily_price_kopeks=100,
                traffic_limit_gb=100,
                device_limit=5,
                allowed_squads=['test-squad'],
            )
        )
        await c.db.commit()
        c.callback.data = 'daily_tariff_switch_confirm:2'
        panel = SimpleNamespace(update_remnawave_user=AsyncMock())
        monkeypatch.setattr(telegram, 'SubscriptionService', lambda: panel)
        monkeypatch.setattr(telegram, 'should_create_panel_account', AsyncMock(return_value=False))
        if fail:
            monkeypatch.setattr(
                telegram,
                'create_transaction',
                AsyncMock(side_effect=asyncio.CancelledError() if fail == 'cancel' else RuntimeError('ledger failed')),
            )
        if fail == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await inspect.unwrap(telegram.confirm_daily_tariff_switch)(c.callback, c.user, c.db, c.state)
            assert not c.db.in_transaction()
        else:
            await inspect.unwrap(telegram.confirm_daily_tariff_switch)(c.callback, c.user, c.db, c.state)
        await c.db.rollback()
        await c.db.refresh(c.user)
        await c.db.refresh(c.sub)
        assert c.user.balance_kopeks == (400000 if fail else 399900)
        assert c.sub.is_trial is bool(fail)
        if fail:
            assert c.sub.end_date == c.end
            panel.update_remnawave_user.assert_not_awaited()
        else:
            assert c.sub.tariff_id == 2 and c.sub.autopay_enabled is True
            assert c.sub.traffic_used_gb == 0
            assert c.sub.device_limit == 5
            assert panel.update_remnawave_user.await_args.kwargs['reset_traffic'] is True
