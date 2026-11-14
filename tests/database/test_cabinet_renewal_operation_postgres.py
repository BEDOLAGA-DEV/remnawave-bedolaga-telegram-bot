"""A lost renewal response must never debit the wallet a second time."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.cabinet.routes.subscription_modules.renewal import renew_subscription
from app.cabinet.schemas.subscription import RenewalRequest
from app.config import settings
from app.database.models import Base, Subscription, SubscriptionRenewalOperation, Tariff, Transaction, User
from app.services import subscription_renewal_service as renewal
from app.services.pricing_engine import RenewalPricing
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('lost_response', [False, True, 'before_commit', 'insufficient'])
@pytest.mark.parametrize('via_cart', [False, True])
async def test_committed_renewal_replay_is_free(postgres_database, monkeypatch, lost_response, via_cart):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
    monkeypatch.setattr(renewal, 'SubscriptionService', lambda: panel)
    pricing = RenewalPricing(
        base_price=1000,
        servers_price=0,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=1000,
        period_days=30,
        is_tariff_mode=True,
        breakdown={},
    )
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.renewal.pricing_engine.calculate_renewal_price',
        AsyncMock(return_value=pricing),
    )
    old_end = datetime.now(UTC) + timedelta(days=5)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, retry_db):
        user = User(id=42, telegram_id=42, language='ru', status='active', balance_kopeks=10000)
        tariff = Tariff(
            id=1, name='Test', is_active=True, traffic_limit_gb=100, device_limit=2, period_prices={'30': 1000}
        )
        sub = Subscription(
            id=7,
            user_id=42,
            tariff_id=1,
            status='active',
            is_trial=False,
            start_date=old_end - timedelta(days=30),
            end_date=old_end,
            remnawave_short_id='masked-7',
            connected_squads=[],
        )
        db.add_all([user, tariff, sub])
        await db.commit()
        request = RenewalRequest(operation_id=uuid4(), expected_end_date=old_end, period_days=30, subscription_id=7)
        if lost_response == 'insufficient':
            from app.services.user_cart_service import user_cart_service

            save = AsyncMock(return_value=True)
            monkeypatch.setattr(user_cart_service, 'save_user_cart', save)
            user.balance_kopeks = 0
            await db.commit()
            with pytest.raises(HTTPException) as exc:
                await renew_subscription(request, user=user, db=db, subscription_id=7)
            assert exc.value.status_code == 402
            cart = save.await_args.args[1]
            assert cart['renewal_operation_id'] == str(request.operation_id)
            assert cart['renewal_expected_end_date'] == old_end.isoformat()
            await db.rollback()
            assert await db.scalar(select(func.count()).select_from(SubscriptionRenewalOperation)) == 0
            user = await db.get(User, 42)
            user.balance_kopeks = 10000
            await db.commit()
        elif lost_response == 'before_commit':
            with monkeypatch.context() as patch:
                patch.setattr(renewal, 'create_transaction', AsyncMock(side_effect=RuntimeError('before commit')))
                with pytest.raises(RuntimeError, match='before commit'):
                    await renew_subscription(request, user=user, db=db, subscription_id=7)
            assert await db.scalar(select(User.balance_kopeks).where(User.id == 42)) == 10000
            assert await db.scalar(select(func.count()).select_from(SubscriptionRenewalOperation)) == 0
            await db.rollback()
        elif lost_response:
            with monkeypatch.context() as patch:
                patch.setattr(
                    'app.database.crud.transaction.emit_transaction_side_effects',
                    AsyncMock(side_effect=RuntimeError('lost after commit')),
                )
                with pytest.raises(RuntimeError, match='lost after commit'):
                    await renew_subscription(request, user=user, db=db, subscription_id=7)
        else:
            first = await renew_subscription(request, user=user, db=db, subscription_id=7)
            await db.rollback()  # Release read transactions left by post-commit side effects.
        other_user = await retry_db.get(User, 42)
        if via_cart:
            from app.services import subscription_auto_purchase_service as auto

            monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
            assert await auto._process_single_cart(
                retry_db,
                other_user,
                {
                    'cart_mode': 'extend',
                    'subscription_id': 7,
                    'period_days': 30,
                    'renewal_operation_id': str(request.operation_id),
                    'renewal_expected_end_date': old_end.isoformat(),
                },
            )
        replay = await renew_subscription(request, user=other_user, db=retry_db, subscription_id=7)
        if not lost_response:
            assert replay == first
        assert replay['amount_paid_kopeks'] == 1000
        assert await retry_db.scalar(select(User.balance_kopeks).where(User.id == 42)) == 9000
        assert await retry_db.scalar(select(Subscription.end_date).where(Subscription.id == 7)) == old_end + timedelta(
            days=30
        )
        assert await retry_db.scalar(select(func.count()).select_from(Transaction)) == 1
        assert await retry_db.scalar(select(func.count()).select_from(SubscriptionRenewalOperation)) == 1
        # A different tab cannot evade the guard with a new key and the old snapshot.
        stale = request.model_copy(update={'operation_id': uuid4()})
        with pytest.raises(HTTPException) as exc:
            await renew_subscription(stale, user=other_user, db=retry_db, subscription_id=7)
        assert exc.value.status_code == 409
        # Nor can a caller reuse an existing key for a different period.
        with pytest.raises(HTTPException) as exc:
            await renew_subscription(
                request.model_copy(update={'period_days': 60}), user=other_user, db=retry_db, subscription_id=7
            )
        assert exc.value.status_code == 409


@pytest.mark.parametrize('different_key', [False, True])
@pytest.mark.parametrize('via_cart', [False, True])
async def test_parallel_tabs_charge_once(postgres_database, monkeypatch, different_key, via_cart):
    import asyncio

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(
        renewal,
        'SubscriptionService',
        lambda: SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock()),
    )
    pricing = RenewalPricing(
        base_price=1000,
        servers_price=0,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=1000,
        period_days=30,
        is_tariff_mode=True,
        breakdown={},
    )
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.renewal.pricing_engine.calculate_renewal_price',
        AsyncMock(return_value=pricing),
    )
    end = datetime.now(UTC) + timedelta(days=10)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (left, right):
        left.add_all(
            [
                User(id=1, telegram_id=100001, balance_kopeks=10000, status='active'),
                Tariff(id=1, name='Test', is_active=True, period_prices={'30': 1000}),
                Subscription(
                    id=1,
                    user_id=1,
                    tariff_id=1,
                    status='active',
                    is_trial=False,
                    start_date=end - timedelta(days=30),
                    end_date=end,
                    remnawave_short_id='masked-1',
                    connected_squads=[],
                ),
            ]
        )
        await left.commit()
        request = RenewalRequest(operation_id=uuid4(), expected_end_date=end, period_days=30, subscription_id=1)
        other = request.model_copy(update={'operation_id': uuid4()}) if different_key else request

        async def run(db, payload):
            try:
                user = await db.get(User, 1)
                if via_cart and db is right:
                    from app.services import subscription_auto_purchase_service as auto

                    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
                    return await auto._process_single_cart(
                        db,
                        user,
                        {
                            'cart_mode': 'extend',
                            'subscription_id': 1,
                            'period_days': 30,
                            'renewal_operation_id': str(payload.operation_id),
                            'renewal_expected_end_date': end.isoformat(),
                        },
                    )
                return await renew_subscription(payload, user=user, db=db, subscription_id=1)
            except HTTPException as exc:
                return exc.status_code
            finally:
                await db.rollback()

        results = await asyncio.wait_for(asyncio.gather(run(left, request), run(right, other)), timeout=15)
        if via_cart:
            assert (
                (isinstance(results[0], dict) and results[1] is True)
                if not different_key
                else (
                    (isinstance(results[0], dict) and results[1] is False) or (results[0] == 409 and results[1] is True)
                )
            )
        elif different_key:
            assert sum(isinstance(item, dict) for item in results) == 1
            assert 409 in results
        else:
            assert results[0] == results[1]
        assert await left.scalar(select(User.balance_kopeks).where(User.id == 1)) == 9000
        assert await left.scalar(select(func.count()).select_from(Transaction)) == 1


async def test_legacy_cabinet_cart_cannot_charge_without_identity():
    from app.services.subscription_auto_purchase_service import _process_single_cart

    db = AsyncMock()
    assert (
        await _process_single_cart(
            db, SimpleNamespace(id=42), {'cart_mode': 'extend', 'source': 'cabinet', 'subscription_id': 7}
        )
        is False
    )
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_renewal_receipt_migration_preserves_wallet(postgres_database):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    from tests.database.test_retired_gateway_migration_postgres import _load
    from tests.fixtures.postgres_db import postgres_engine

    migration = _load('0141_subscription_renewal_operations.py')
    async with postgres_engine(postgres_database, list(Base.metadata.sorted_tables)) as engine:
        async with engine.begin() as conn:

            def exercise(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()
                    sync.execute(User.__table__.insert().values(id=42, balance_kopeks=10000))
                    migration.upgrade()
                    assert sync.scalar(select(User.balance_kopeks).where(User.id == 42)) == 10000
                    assert sync.scalar(select(func.count()).select_from(SubscriptionRenewalOperation)) == 0
                    columns = {c['name']: c for c in inspect(sync).get_columns('subscription_renewal_operations')}
                    assert columns['expected_end_date']['type'].timezone
                    assert set(
                        inspect(sync).get_pk_constraint('subscription_renewal_operations')['constrained_columns']
                    ) == {'user_id', 'operation_id'}
                    migration.downgrade()
                    assert sync.scalar(select(User.balance_kopeks).where(User.id == 42)) == 10000
                    migration.upgrade()

            await conn.run_sync(exercise)
