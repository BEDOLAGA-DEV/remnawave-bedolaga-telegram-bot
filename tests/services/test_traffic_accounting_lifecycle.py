from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import Subscription
from app.services.traffic_accounting import Allowance, TrafficAccount
from app.services.traffic_accounting_store import initialize_account, load_account
from tests.services.test_traffic_accounting_service import END, NOW, PanelDouble
from tests.services.test_traffic_accounting_store import database


async def seed(db, *, remaining=0):
    state = await initialize_account(
        db,
        1,
        TrafficAccount('123@2025-11-16T00:00:00+00:00', 100, 30, (Allowance('purchase:1', remaining, END),)),
        strategy='NO_RESET',
        timezone='UTC',
        anchor=NOW,
        next_refresh_at=None,
    )
    state.phase = 'ready'
    sub = await db.get(Subscription, 1)
    sub.traffic_accounting_mode = 'managed'
    sub.remnawave_id = 123
    sub.status = 'active'
    await db.commit()


async def test_generic_enable_does_not_open_exhausted_managed_account(monkeypatch):
    from app.services.traffic_accounting_lifecycle import try_enable_managed_account

    async with database(monkeypatch) as db:
        await seed(db)
        panel = PanelDouble()
        panel.enable_user = AsyncMock()
        handled, enabled = await try_enable_managed_account(db, panel, 123, now=NOW)
        assert handled and not enabled
        panel.enable_user.assert_not_awaited()


async def test_generic_enable_keeps_nonquota_admin_disable(monkeypatch):
    from app.services.traffic_accounting_lifecycle import try_enable_managed_account

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        panel = PanelDouble()
        panel.user.status = 'DISABLED'
        panel.enable_user = AsyncMock()
        handled, enabled = await try_enable_managed_account(db, panel, 123, now=NOW)
        assert handled and not enabled
        panel.enable_user.assert_not_awaited()
        assert (await db.get(Subscription, 1)).status == 'disabled'


async def test_planned_delete_commits_last_usage_before_external_delete(monkeypatch):
    from app.services.traffic_accounting_lifecycle import checkpoint_managed_deletion

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        panel = PanelDouble()
        panel.user.user_traffic.lifetime_used_traffic_bytes = 150
        panel.user.user_traffic.used_traffic_bytes = 80

        async def remove(panel_id):
            async with factory() as verify:
                account, state = await load_account(verify, 1)
                assert account.remaining_bytes(NOW) == 50
                assert state.phase == 'suspended'
            raise TimeoutError('ambiguous delete')

        panel.delete_user = remove
        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        import pytest

        with pytest.raises(TimeoutError):
            await checkpoint_managed_deletion(factory, panel, 123, now=NOW)
            await panel.delete_user(123)
        await db.rollback()
        account, state = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == 50
        assert state.phase == 'suspended'

        panel.user.user_traffic.lifetime_used_traffic_bytes += 20
        panel.user.user_traffic.used_traffic_bytes += 20
        await db.rollback()
        await checkpoint_managed_deletion(factory, panel, 123, now=NOW)
        account, state = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == 30


async def test_explicit_admin_activation_survives_nonquota_panel_disable(monkeypatch):
    from app.services.traffic_accounting_delivery import deliver_account_projection
    from app.services.traffic_accounting_service import stage_authorized_activation

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        sub = await db.get(Subscription, 1)
        sub.status = 'disabled'
        await db.commit()
        sub = await stage_authorized_activation(db, sub)
        sub.status = 'active'
        await db.commit()
        panel = PanelDouble()
        panel.user.status = 'DISABLED'
        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        await deliver_account_projection(factory, panel, 1, now=NOW)
        await db.rollback()
        _, state = await load_account(db, 1)
        assert not state.activation_requested
        assert panel.writes[-1]['status'].value == 'ACTIVE'


async def test_delivery_marks_only_first_quota_exhaustion_as_grace_candidate(monkeypatch):
    from app.services.traffic_accounting_delivery import deliver_account_projection

    async with database(monkeypatch) as db:
        await seed(db)
        panel = PanelDouble()
        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        await deliver_account_projection(factory, panel, 1, now=NOW)
        await db.rollback()
        sub = await db.get(Subscription, 1)
        assert sub.grace_candidate_reason == 'limited'
        assert sub.grace_candidate_at.replace(tzinfo=NOW.tzinfo) == NOW


async def test_usage_exhausted_between_delivery_reads_marks_grace_candidate(monkeypatch):
    from app.services.traffic_accounting_delivery import deliver_account_projection

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        panel = PanelDouble()
        reads = 0

        async def read(panel_id):
            nonlocal reads
            reads += 1
            if reads >= 2:
                panel.user.user_traffic.lifetime_used_traffic_bytes = 200
                panel.user.user_traffic.used_traffic_bytes = 130
            return panel.user

        panel.get_user_by_id = read
        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        await deliver_account_projection(factory, panel, 1, now=NOW)
        await db.rollback()
        sub = await db.get(Subscription, 1)
        assert sub.status == 'limited'
        assert sub.grace_candidate_reason == 'limited'
        assert sub.grace_candidate_at.replace(tzinfo=NOW.tzinfo) == NOW


async def test_explicit_activation_rollback_cannot_unban_account(monkeypatch):
    from app.services.traffic_accounting_delivery import deliver_account_projection
    from app.services.traffic_accounting_service import stage_authorized_activation

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        sub = await db.get(Subscription, 1)
        sub.status = 'disabled'
        await db.commit()
        sub = await stage_authorized_activation(db, sub)
        sub.status = 'active'
        await db.rollback()
        panel = PanelDouble()
        panel.user.status = 'DISABLED'
        factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
        await deliver_account_projection(factory, panel, 1, now=NOW)
        status = panel.writes[-1].get('status')
        assert status is None or status.value == 'DISABLED'
        assert panel.user.status == 'DISABLED'


async def test_cleanup_retires_ledger_and_pending_grants_without_erasing_history(monkeypatch):
    from app.database.models import TrafficBaseGrantIntent
    from app.services.traffic_accounting_lifecycle import retire_managed_account

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        db.add(
            TrafficBaseGrantIntent(
                subscription_id=1,
                operation_key='paid:1',
                base_bytes=500,
                target_unlimited=False,
                reset_base=True,
                business_strategy='MONTH',
                requested_at=NOW,
                expires_at=END,
                state='pending',
            )
        )
        await db.commit()
        await retire_managed_account(db, 1, now=NOW)
        await db.commit()
        account, state = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == 0
        assert len(account.allowances) == 1
        assert account.allowances[0].granted_bytes == 100
        assert state.phase == 'suspended'
        assert state.next_refresh_at is None
        assert state.base_budget_bytes == 0
        from sqlalchemy import select

        intent = await db.scalar(select(TrafficBaseGrantIntent))
        assert intent.state == 'superseded'


async def test_telegram_admin_activation_stages_durable_intent(monkeypatch):
    from app.handlers.admin import users
    from app.services.subscription_service import SubscriptionService

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        sub = await db.get(Subscription, 1)
        sub.status = 'disabled'
        await db.commit()
        monkeypatch.setattr(users, '_resolve_admin_subscription', AsyncMock(return_value=sub))
        monkeypatch.setattr('app.services.grace_access_echo.undo_grace_overlay_echo', AsyncMock())
        monkeypatch.setattr('app.database.crud.subscription.reconcile_tariff_traffic_limit', AsyncMock())
        monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock())
        monkeypatch.setattr(
            'app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *args: None
        )
        assert await users._activate_user_subscription(db, 1, 2, 1)
        await db.rollback()
        _, state = await load_account(db, 1)
        assert state.activation_requested
        assert (await db.get(Subscription, 1)).status == 'active'


@pytest.mark.parametrize('entry', ['cabinet', 'bulk'])
async def test_cabinet_admin_activation_stages_durable_intent(monkeypatch, entry):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from app.cabinet.routes import admin_bulk_actions, admin_users
    from app.cabinet.schemas.users import UpdateSubscriptionRequest
    from app.database.models import User

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        sub = await db.get(Subscription, 1)
        sub.status = 'disabled'
        sub.end_date = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        user = await db.get(User, 1)
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr(admin_users, '_get_owned_subscription_or_404', AsyncMock(return_value=sub))
        monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock())
        monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
        monkeypatch.setattr(
            'app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *args: None
        )
        if entry == 'cabinet':
            result = await admin_users.update_user_subscription(
                user_id=1,
                request=UpdateSubscriptionRequest(action='activate', subscription_id=1),
                admin=SimpleNamespace(id=2),
                db=db,
            )
        else:
            monkeypatch.setattr(admin_bulk_actions, '_sync_subscription_to_panel', AsyncMock())
            monkeypatch.setattr(admin_bulk_actions, 'reconcile_tariff_traffic_limit', AsyncMock())
            result = await admin_bulk_actions._do_activate_subscription(
                db, user, SimpleNamespace(), dry_run=False, sub_override=sub
            )
        assert result.success
        await db.rollback()
        _, state = await load_account(db, 1)
        assert state.activation_requested
        assert (await db.get(Subscription, 1)).status == 'active'
        assert sub.pending_panel_expire_at == sub.end_date
        assert sub.pending_panel_expire_at > datetime.now(UTC)


async def test_managed_admin_cannot_forge_usage_or_partially_change_limit(monkeypatch):
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException

    from app.cabinet.routes import admin_users
    from app.cabinet.schemas.users import UpdateSubscriptionRequest
    from app.database.models import User

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        sub = await db.get(Subscription, 1)
        original_limit = sub.traffic_limit_gb
        user = await db.get(User, 1)
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr(admin_users, '_get_owned_subscription_or_404', AsyncMock(return_value=sub))
        with pytest.raises(HTTPException) as error:
            await admin_users.update_user_subscription(
                user_id=1,
                request=UpdateSubscriptionRequest(
                    action='set_traffic', subscription_id=1, traffic_limit_gb=200, traffic_used_gb=0
                ),
                admin=SimpleNamespace(id=2),
                db=db,
            )
        assert error.value.status_code == 409
        assert sub.traffic_limit_gb == original_limit
        account, state = await load_account(db, 1)
        assert state.revision == 0
        assert account.remaining_bytes(NOW) == 100


@pytest.mark.parametrize('fail_staging', [False, True])
async def test_managed_admin_limit_change_stages_resize_without_reset(monkeypatch, fail_staging):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.cabinet.routes import admin_users
    from app.cabinet.schemas.users import UpdateSubscriptionRequest
    from app.database.models import TrafficBaseGrantIntent, User
    from app.services.subscription_service import SubscriptionService

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        sub = await db.get(Subscription, 1)
        original_limit = sub.traffic_limit_gb
        sub.purchased_traffic_gb = 10
        await db.commit()
        user = await db.get(User, 1)
        await db.refresh(user, ['subscriptions'])
        panel = PanelDouble()

        @asynccontextmanager
        async def client(self):
            yield panel

        monkeypatch.setattr(SubscriptionService, 'get_api_client', client)
        monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
        monkeypatch.setattr(admin_users, '_get_owned_subscription_or_404', AsyncMock(return_value=sub))
        monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock())
        monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
        monkeypatch.setattr(
            'app.services.traffic_accounting_service.schedule_projection_after_commit', lambda *args: None
        )
        if fail_staging:
            monkeypatch.setattr(
                'app.services.traffic_base_grant_service.stage_paid_base_grant',
                AsyncMock(side_effect=RuntimeError('intent storage unavailable')),
            )
            with pytest.raises(RuntimeError, match='intent storage unavailable'):
                await admin_users.update_user_subscription(
                    user_id=1,
                    request=UpdateSubscriptionRequest(action='set_traffic', subscription_id=1, traffic_limit_gb=200),
                    admin=SimpleNamespace(id=2),
                    db=db,
                )
            await db.rollback()
            assert (await db.get(Subscription, 1)).traffic_limit_gb == original_limit
            assert await db.scalar(select(TrafficBaseGrantIntent)) is None
            return
        result = await admin_users.update_user_subscription(
            user_id=1,
            request=UpdateSubscriptionRequest(action='set_traffic', subscription_id=1, traffic_limit_gb=200),
            admin=SimpleNamespace(id=2),
            db=db,
        )
        assert result.success
        await db.rollback()
        intent = await db.scalar(select(TrafficBaseGrantIntent))
        assert intent.base_bytes == 190 * 1024**3
        assert not intent.reset_base
        assert intent.state == 'pending'
        assert (await db.get(Subscription, 1)).traffic_limit_gb == 200


@pytest.mark.parametrize('fail_staging', [False, True])
async def test_admin_classic_trial_purchase_stages_unlimited_conversion_atomically(monkeypatch, fail_staging):
    import inspect
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.database.models import TrafficBaseGrantIntent, Transaction, User
    from app.handlers.admin import users
    from app.services.remnawave_service import RemnaWaveService

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        sub = await db.get(Subscription, 1)
        sub.is_trial = True
        sub.traffic_limit_gb = 100
        sub.end_date = datetime.now(UTC) + timedelta(days=1)
        user = await db.get(User, 1)
        user.balance_kopeks = 10000
        await db.commit()
        monkeypatch.setattr(
            users.UserService, 'get_user_profile', AsyncMock(return_value={'user': user, 'subscription': sub})
        )
        monkeypatch.setattr(users, '_admin_buy_periods', AsyncMock(return_value=[30]))
        monkeypatch.setattr(users, '_calculate_subscription_period_price', AsyncMock(return_value=1000))
        monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
        monkeypatch.setattr(
            RemnaWaveService, 'get_api_client', lambda self: (_ for _ in ()).throw(RuntimeError('offline'))
        )
        if fail_staging:
            monkeypatch.setattr(
                'app.services.traffic_base_grant_service.stage_paid_base_grant',
                AsyncMock(side_effect=RuntimeError('intent storage unavailable')),
            )
        callback = SimpleNamespace(
            data='admin_buy_sub_execute_1_30_1000_s1',
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
            bot=None,
        )
        await inspect.unwrap(users.admin_buy_subscription_execute)(callback, user, db)
        await db.rollback()
        user = await db.get(User, 1)
        sub = await db.get(Subscription, 1)
        intent = await db.scalar(select(TrafficBaseGrantIntent))
        if fail_staging:
            assert user.balance_kopeks == 10000
            assert sub.is_trial
            assert sub.traffic_limit_gb == 100
            assert intent is None
            assert await db.scalar(select(Transaction)) is None
        else:
            assert user.balance_kopeks == 9000
            assert sub.traffic_limit_gb == 0
            assert not sub.is_trial
            assert intent.target_unlimited
            assert not intent.reset_base


async def test_admin_revoke_retires_only_selected_package_remaining(monkeypatch):
    from app.services.traffic_accounting_service import lock_accounting_subscription, revoke_purchase_locked
    from app.services.traffic_accounting_store import save_account

    async with database(monkeypatch) as db:
        await seed(db, remaining=100)
        account, state = await load_account(db, 1)
        await save_account(db, 1, account.add(Allowance('purchase:2', 70, END)), expected_revision=state.revision)
        await db.commit()
        sub, _ = await lock_accounting_subscription(db, 1)
        panel = PanelDouble()
        panel.user.user_traffic.lifetime_used_traffic_bytes = 150
        panel.user.user_traffic.used_traffic_bytes = 80
        account = await revoke_purchase_locked(db, sub, 1, panel.user, now=NOW)
        assert account.remaining_bytes(NOW) == 70
        assert account.allowances[0].key == 'purchase:1'
        assert account.allowances[0].remaining_bytes == 0
        assert account.allowances[0].granted_bytes == 100
        assert len(account.allowances) == 2
        await revoke_purchase_locked(db, sub, 1, panel.user, now=NOW)
        account, _ = await load_account(db, 1)
        assert account.remaining_bytes(NOW) == 70
