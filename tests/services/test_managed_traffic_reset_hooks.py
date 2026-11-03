"""Managed reset events reconcile counters without resurrecting paid packages."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.remnawave_webhook_service import RemnaWaveWebhookService


async def test_managed_reset_webhook_does_not_zero_usage_or_reactivate(monkeypatch):
    from app.services import traffic_accounting_service as accounting

    sub = SimpleNamespace(id=1, traffic_accounting_mode='managed', status='active', traffic_used_gb=37)
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(accounting, 'refresh_managed_account', AsyncMock())
    service = RemnaWaveWebhookService.__new__(RemnaWaveWebhookService)
    service._notify_user = AsyncMock()
    service._get_subscription_keyboard = lambda user: None
    await service._handle_user_traffic_reset(db, SimpleNamespace(id=2), sub, {})
    assert sub.traffic_used_gb == 37
    assert sub.status == 'active'
    service._notify_user.assert_not_awaited()


async def test_managed_daily_intent_confirms_base_grant_without_physical_reset(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.services import paid_traffic_reset_service as resets, traffic_accounting_service as accounting

    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=1,
        user_id=2,
        traffic_accounting_mode='managed',
        status='active',
        is_trial=False,
        is_daily_paused=False,
        grace_session_open=False,
        end_date=now + timedelta(days=1),
        last_daily_charge_at=now,
        remnawave_id=3,
    )
    user = SimpleNamespace(id=2, status='active', remnawave_id=3)
    intent = SimpleNamespace(
        id=4,
        subscription_id=1,
        user_id=2,
        state='pending',
        charge_at=now,
        operation_key='daily:1:masked',
        target_panel_id=None,
        attempted_at=None,
    )
    db = SimpleNamespace(get=AsyncMock(side_effect=[intent, sub]), rollback=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(resets, '_locked', AsyncMock(return_value=(user, sub, intent, [intent])))
    monkeypatch.setattr(resets, 'find_foreign_panel_owner', AsyncMock(return_value=None))
    monkeypatch.setattr(accounting, 'lock_accounting_subscription', AsyncMock(return_value=(sub, False)))
    monkeypatch.setattr(accounting, 'refresh_managed_account', AsyncMock())
    # Any entry into the physical reset machinery is a regression.
    db.scalars = AsyncMock(side_effect=AssertionError('managed intent entered physical reset path'))
    assert await resets.deliver_daily_reset(db, intent.id)
    assert intent.state == 'confirmed'
    assert intent.activation_pending is False
    assert intent.attempted_at is None


async def test_paid_managed_reset_is_atomic_and_same_confirmation_charges_once(monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.database.models import Subscription, Transaction, User
    from app.services import (
        paid_traffic_reset_service as resets,
        subscription_service,
        traffic_accounting_service as accounting,
    )
    from app.services.traffic_accounting import Allowance, TrafficAccount
    from app.services.traffic_accounting_store import initialize_account, load_account
    from tests.services.test_traffic_accounting_store import database

    now = datetime.now(UTC)
    panel = SimpleNamespace(
        id=3,
        created_at=now - timedelta(days=60),
        user_traffic=SimpleNamespace(used_traffic_bytes=20, lifetime_used_traffic_bytes=20),
    )

    class Service:
        @asynccontextmanager
        async def get_api_client(self):
            yield object()

    monkeypatch.setattr(subscription_service, 'SubscriptionService', Service)
    monkeypatch.setattr(accounting, '_fresh_panel', AsyncMock(return_value=panel))
    async with database(monkeypatch) as db:
        user = await db.get(User, 1)
        user.balance_kopeks = 1000
        user.status = 'active'
        sub = await db.get(Subscription, 1)
        sub.traffic_accounting_mode = 'managed'
        sub.is_trial = False
        sub.traffic_limit_gb = 2
        sub.purchased_traffic_gb = 1
        sub.end_date = now + timedelta(days=30)
        state = await initialize_account(
            db,
            1,
            TrafficAccount(
                accounting.read_counters(panel).identity,
                20,
                20,
                (Allowance('base:initial', 0, sub.end_date), Allowance('purchase:old', 12, sub.end_date)),
            ),
            strategy='NO_RESET',
            timezone='UTC',
            anchor=now,
            next_refresh_at=None,
        )
        state.phase = 'ready'
        await db.commit()
        await resets.purchase_managed_reset(db, 1, user_id=1, operation_key='telegram:message-1', price=lambda sub: 100)
        await resets.purchase_managed_reset(db, 1, user_id=1, operation_key='telegram:message-1', price=lambda sub: 100)
        await db.refresh(user)
        assert user.balance_kopeks == 900
        assert len((await db.scalars(select(Transaction))).all()) == 1
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(now) == 1024**3 + 12
        # A failure after the debit must roll back money and grant together.
        import pytest

        from app.database.crud import transaction as transaction_crud

        original_create = transaction_crud.create_transaction
        monkeypatch.setattr(
            transaction_crud, 'create_transaction', AsyncMock(side_effect=RuntimeError('ledger failed'))
        )
        with pytest.raises(RuntimeError, match='ledger failed'):
            await resets.purchase_managed_reset(
                db,
                1,
                user_id=1,
                operation_key='telegram:message-2',
                price=lambda sub: 100,
            )
        await db.refresh(user)
        assert user.balance_kopeks == 900
        assert len((await db.scalars(select(Transaction))).all()) == 1
        monkeypatch.setattr(transaction_crud, 'create_transaction', original_create)
        # A new rendered confirmation is a new purchase, even in the same message.
        await resets.purchase_managed_reset(
            db,
            1,
            user_id=1,
            operation_key='telegram:message-2',
            price=lambda sub: 100,
        )
        await db.refresh(user)
        assert user.balance_kopeks == 800


async def test_managed_admin_sync_never_calls_physical_reset(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from app.cabinet.routes.admin_users import _sync_subscription_to_panel
    from app.services import panel_sync, remnawave_service

    api = SimpleNamespace(reset_user_traffic=AsyncMock())

    class Service:
        is_configured = True

        @asynccontextmanager
        async def get_api_client(self):
            yield api

    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', Service)
    monkeypatch.setattr(
        panel_sync, 'push_subscription', AsyncMock(return_value=SimpleNamespace(panel_user_id=3, action='updated'))
    )
    sub = SimpleNamespace(id=1, remnawave_id=3, traffic_accounting_mode='managed')
    db = SimpleNamespace(refresh=AsyncMock(), commit=AsyncMock())
    await _sync_subscription_to_panel(db, MagicMock(id=2), sub, reset_traffic=True)
    api.reset_user_traffic.assert_not_awaited()


async def test_open_grace_rejects_admin_mutation_before_panel_read(monkeypatch):
    import pytest

    from app.services import paid_traffic_reset_service as resets, traffic_accounting_service as accounting
    from app.services.traffic_accounting import AccountingConflict

    sub = SimpleNamespace(id=1, traffic_accounting_mode='managed')
    monkeypatch.setattr(accounting, 'lock_accounting_subscription', AsyncMock(return_value=(sub, True)))
    monkeypatch.setattr(accounting, '_fresh_panel', AsyncMock(side_effect=AssertionError('must reject before GET')))
    with pytest.raises(AccountingConflict, match='grace'):
        await resets.prepare_admin_tariff_accounting(SimpleNamespace(), sub)


async def test_old_admin_confirmation_cannot_regrant_after_another_switch(monkeypatch):
    from app.database.crud import subscription as sub_crud, transaction as transaction_crud
    from app.handlers.admin import users as handlers
    from app.services import paid_traffic_reset_service as resets, traffic_accounting_service as accounting

    sub = SimpleNamespace(id=42, user_id=7, tariff_id=1, traffic_accounting_mode='managed')
    user = SimpleNamespace(id=7)
    tariff = SimpleNamespace(id=2)
    callback = SimpleNamespace(data='astc:42:2:' + 'a' * 16, answer=AsyncMock())
    monkeypatch.setattr(sub_crud, 'get_subscription_by_id', AsyncMock(return_value=sub))
    monkeypatch.setattr(sub_crud, 'get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    monkeypatch.setattr(handlers, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(handlers, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(resets, 'prepare_admin_tariff_accounting', AsyncMock(return_value=(sub, object())))
    grant, transaction = AsyncMock(), AsyncMock()
    monkeypatch.setattr(accounting, 'change_managed_tariff_locked', grant)
    monkeypatch.setattr(transaction_crud, 'create_transaction', transaction)
    handler = handlers.confirm_admin_tariff_change
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    await handler(callback, SimpleNamespace(id=99), SimpleNamespace(scalar=AsyncMock(return_value=123)))
    assert sub.tariff_id == 1
    grant.assert_not_awaited()
    transaction.assert_not_awaited()
    assert 'уже применено' in callback.answer.await_args.args[0]


async def test_rejected_admin_switch_does_not_cancel_recurring(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.cabinet.routes import admin_users as route
    from app.services import cashera_recurring_cancel, paid_traffic_reset_service as resets
    from app.services.payment import lava, platega
    from app.services.traffic_accounting import AccountingConflict

    sub = SimpleNamespace(id=42, user_id=7, tariff_id=1, is_active=True, traffic_accounting_mode='managed')
    user = SimpleNamespace(id=7, subscriptions=[sub])
    monkeypatch.setattr(route, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(route.settings, 'MULTI_TARIFF_ENABLED', False)
    cancelled = AsyncMock()
    monkeypatch.setattr(lava, 'cancel_lava_recurring_for_subscription_safe', cancelled)
    monkeypatch.setattr(platega, 'cancel_platega_recurring_for_subscription_safe', cancelled)
    monkeypatch.setattr(cashera_recurring_cancel, 'cancel_cashera_recurring_for_subscription_safe', cancelled)
    request = SimpleNamespace(action='change_tariff', tariff_id=2, subscription_id=None)
    monkeypatch.setattr(route, 'get_tariff_by_id', AsyncMock(return_value=None))
    with pytest.raises(HTTPException):
        await route.update_user_subscription(7, request, admin=SimpleNamespace(id=99), db=SimpleNamespace())
    cancelled.assert_not_awaited()
    monkeypatch.setattr(route, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(id=2)))
    monkeypatch.setattr(resets, 'prepare_admin_tariff_accounting', AsyncMock(side_effect=AccountingConflict('grace')))
    with pytest.raises(AccountingConflict):
        await route.update_user_subscription(7, request, admin=SimpleNamespace(id=99), db=SimpleNamespace())
    cancelled.assert_not_awaited()


async def test_open_grace_rejects_paid_reset_before_charge(monkeypatch):
    import pytest

    from app.services import paid_traffic_reset_service as resets, traffic_accounting_service as accounting
    from app.services.traffic_accounting import AccountingConflict

    sub = SimpleNamespace(id=1)
    monkeypatch.setattr(accounting, 'lock_accounting_subscription', AsyncMock(return_value=(sub, True)))
    db = SimpleNamespace(rollback=AsyncMock())
    with pytest.raises(AccountingConflict, match='grace'):
        await resets.purchase_managed_reset(db, 1, user_id=1, operation_key='test-grace', price=lambda sub: 100)


@pytest.mark.parametrize(
    ('event', 'local_status', 'panel_status', 'owned', 'remaining', 'grace', 'expected'),
    [
        ('disabled', 'limited', 'DISABLED', True, 0, False, 'limited'),
        ('disabled', 'active', 'ACTIVE', False, 10, False, 'active'),
        ('enabled', 'disabled', 'ACTIVE', False, 10, False, 'disabled'),
        ('disabled', 'active', 'DISABLED', False, 10, False, 'disabled'),
        ('limited', 'active', 'ACTIVE', False, 10, False, 'active'),
        ('limited', 'active', 'ACTIVE', False, 0, False, 'limited'),
        ('disabled', 'active', 'DISABLED', False, 0, True, 'active'),
    ],
)
async def test_managed_status_webhook_uses_fresh_quota_ownership(
    monkeypatch, event, local_status, panel_status, owned, remaining, grace, expected
):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta

    from app.services import (
        subscription_service,
        traffic_accounting_service as accounting,
        traffic_accounting_store as store,
    )
    from app.services.remnawave_webhook_service import grace_access_runtime
    from app.services.traffic_accounting import Allowance, TrafficAccount

    consider = AsyncMock()
    monkeypatch.setattr(grace_access_runtime, 'consider_candidate', consider)
    now = datetime.now(UTC)
    from app.database.models import Subscription, User

    sub = Subscription(
        id=1, user_id=2, traffic_accounting_mode='managed', status=local_status, user=User(id=2, status='active')
    )
    panel = SimpleNamespace(status=panel_status)
    account = TrafficAccount('1', 100, 100, (Allowance('base:1', remaining, now + timedelta(days=1)),))
    state = SimpleNamespace(quota_disabled=owned, grace_excluded=False, phase='ready')
    monkeypatch.setattr(accounting, 'lock_accounting_subscription', AsyncMock(return_value=(sub, grace)))
    monkeypatch.setattr(accounting, '_fresh_panel', AsyncMock(return_value=panel))
    monkeypatch.setattr(accounting, 'reconcile_account_locked', AsyncMock(return_value=account))
    monkeypatch.setattr(store, 'load_account', AsyncMock(return_value=(account, state)))
    monkeypatch.setattr(accounting, 'schedule_projection_after_commit', lambda *args: None)

    class Service:
        @asynccontextmanager
        async def get_api_client(self):
            yield object()

    monkeypatch.setattr(subscription_service, 'SubscriptionService', Service)
    handler = RemnaWaveWebhookService.__new__(RemnaWaveWebhookService)
    handler._notify_user = AsyncMock()
    handler._get_subscription_keyboard = lambda user: None
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    await getattr(handler, f'_handle_user_{event}')(db, SimpleNamespace(id=2), sub, {'status': 'DISABLED'})
    assert sub.status == expected
    if local_status == 'active' and expected == 'limited':
        assert sub.grace_candidate_reason == 'limited'
        first_candidate_at = sub.grace_candidate_at
        consider.assert_awaited_once()
        await getattr(handler, f'_handle_user_{event}')(db, SimpleNamespace(id=2), sub, {})
        assert sub.grace_candidate_at == first_candidate_at
        consider.assert_awaited_once()
    else:
        consider.assert_not_awaited()


@pytest.fixture(autouse=True)
def no_cispay_binding_in_legacy_unit_scenarios(monkeypatch):
    """These scenarios have no CisPay consent; live-agreement guards use real PostgreSQL tests."""
    from unittest.mock import AsyncMock

    from app.services import cispay_recurrent_guard

    monkeypatch.setattr(cispay_recurrent_guard, 'has_live_cispay', AsyncMock(return_value=False))
    monkeypatch.setattr(cispay_recurrent_guard, 'has_conflicting_cispay', AsyncMock(return_value=False))
