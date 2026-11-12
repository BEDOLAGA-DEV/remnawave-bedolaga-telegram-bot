"""A paid renewal converts trial terms atomically, including same-tariff trials."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.cabinet.routes.subscription_modules.renewal import renew_subscription
from app.cabinet.schemas.subscription import RenewalRequest
from app.config import CLASSIC_PERIOD_PRICES, settings
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.services import subscription_auto_purchase_service as auto, subscription_renewal_service as renewal
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('classic', [False, True])
@pytest.mark.parametrize('flow', ['cabinet', 'cart'])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('carry', [False, True])
@pytest.mark.parametrize('fail', [False, True])
async def test_cabinet_trial_renewal_applies_paid_terms(
    postgres_database, monkeypatch, multi, carry, fail, flow, classic
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic' if classic else 'tariffs')
    monkeypatch.setattr(settings, 'DEFAULT_DEVICE_LIMIT', 5)
    monkeypatch.setattr(settings, 'TRAFFIC_SELECTION_MODE', 'fixed')
    monkeypatch.setattr(settings, 'FIXED_TRAFFIC_LIMIT_GB', 100)
    monkeypatch.setattr(settings, 'TRAFFIC_PACKAGES_CONFIG', '100:0:true')
    monkeypatch.setitem(CLASSIC_PERIOD_PRICES, 30, 1000)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', carry)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
    monkeypatch.setattr(renewal, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(auto, 'SubscriptionService', lambda: panel)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    monkeypatch.setattr(auto, 'clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr(auto, '_reject_unavailable_saved_squads', AsyncMock(return_value=False))
    now = datetime.now(UTC)
    old_end = now + timedelta(days=2)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        user = User(id=1, telegram_id=1, language='ru', status='active', balance_kopeks=10000)
        tariff = Tariff(
            id=1,
            name='Paid',
            is_active=True,
            traffic_limit_gb=100,
            device_limit=5,
            period_prices={'30': 1000},
            allowed_squads=['paid-squad'],
        )
        sub = Subscription(
            id=1,
            user_id=1,
            tariff_id=None if classic else 1,
            is_trial=True,
            status='active',
            start_date=now - timedelta(days=1),
            end_date=old_end,
            traffic_limit_gb=1,
            traffic_used_gb=0.5,
            device_limit=1,
            connected_squads=['trial-squad'],
        )
        db.add_all([user, tariff, sub])
        await db.commit()
        request = RenewalRequest(operation_id=uuid4(), expected_end_date=old_end, period_days=30, subscription_id=1)
        if flow == 'cart':
            if fail:
                monkeypatch.setattr(auto, 'create_transaction', AsyncMock(side_effect=RuntimeError('ledger failed')))
            result = await auto._auto_extend_subscription(
                db,
                user,
                {
                    'subscription_id': 1,
                    'tariff_id': None if classic else 1,
                    'period_days': 30,
                    'device_limit': 1,
                    'traffic_limit_gb': 1,
                    'allowed_squads': ['trial-squad'],
                },
            )
            assert result is not fail
        elif fail:
            monkeypatch.setattr(renewal, 'create_transaction', AsyncMock(side_effect=RuntimeError('ledger failed')))
            with pytest.raises(RuntimeError, match='ledger failed'):
                await renew_subscription(request, user, db, 1)
        else:
            await renew_subscription(request, user, db, 1)
            # Same checkout replay must not convert/charge a second time.
            await renew_subscription(request, user, db, 1)
        saved = await observer.get(Subscription, 1)
        owner = await observer.get(User, 1)
        assert saved.is_trial is fail
        assert owner.balance_kopeks == (10000 if fail else 9000)
        assert saved.traffic_limit_gb == (1 if fail else 100)
        assert saved.device_limit == (1 if fail else 5)
        assert saved.connected_squads == (['trial-squad'] if fail or classic else ['paid-squad'])
        assert saved.traffic_used_gb == (0.5 if fail else 0)
        transactions = list(await observer.scalars(select(Transaction)))
        assert len(transactions) == (0 if fail else 1)
        if fail:
            assert saved.end_date == old_end
            panel.create_remnawave_user.assert_not_awaited()
        else:
            expected = (old_end if carry else now) + timedelta(days=30)
            assert abs((saved.end_date - expected).total_seconds()) < 10
            called = panel.update_remnawave_user if flow == 'cart' else panel.create_remnawave_user
            assert called.await_args.kwargs['reset_traffic'] is True


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('trial_tariff', [0, 1])
@pytest.mark.parametrize('paid_trial', [False, True])
async def test_trial_activation_then_tariff_purchase(postgres_database, monkeypatch, multi, trial_tariff, paid_trial):
    from app.cabinet.routes.subscription_modules import purchase
    from app.cabinet.schemas.subscription import TariffPurchaseRequest

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', trial_tariff)
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', paid_trial)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)
    monkeypatch.setattr(settings, 'TRIAL_DEVICE_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_TRAFFIC_LIMIT_GB', 1)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    panel = SimpleNamespace(is_configured=False, create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
    monkeypatch.setattr(purchase, 'SubscriptionService', lambda: panel)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        user = User(id=1, telegram_id=1, language='ru', status='active', balance_kopeks=10000)
        db.add_all(
            [
                user,
                Tariff(
                    id=1,
                    name='Paid',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=5,
                    period_prices={'30': 1000},
                    allowed_squads=[],
                ),
            ]
        )
        await db.commit()
        await purchase.activate_trial(user=user, db=db, request=None)
        trial = await db.scalar(select(Subscription).where(Subscription.user_id == 1))
        trial_id = trial.id
        assert trial.is_trial is True
        assert trial.tariff_id == (trial_tariff or None)
        await db.refresh(user)
        assert user.balance_kopeks == 10000 - (1000 if paid_trial else 0)
        result = await purchase.purchase_tariff(TariffPurchaseRequest(tariff_id=1, period_days=30), user, db)
        assert result['success'] is True
        saved = await observer.get(Subscription, trial_id)
        assert saved.is_trial is False and saved.tariff_id == 1
        assert saved.traffic_limit_gb == 100 and saved.device_limit == 5
        assert len(list(await observer.scalars(select(Subscription)))) == 1
        owner = await observer.get(User, 1)
        assert owner.balance_kopeks == 9000 - (1000 if paid_trial else 0)


async def test_expired_same_tariff_revival_applies_conversion_defaults(postgres_database, monkeypatch):
    from app.database.crud.subscription import create_paid_subscription

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'DEFAULT_AUTOPAY_ENABLED', True)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, observer):
        now = datetime.now(UTC)
        db.add_all(
            [
                User(id=1, telegram_id=1, status='active'),
                Tariff(
                    id=1, name='Paid', is_active=True, traffic_limit_gb=100, device_limit=5, period_prices={'30': 1000}
                ),
                Subscription(
                    id=1,
                    user_id=1,
                    tariff_id=1,
                    is_trial=True,
                    status='expired',
                    start_date=now - timedelta(days=3),
                    end_date=now - timedelta(days=1),
                    traffic_limit_gb=1,
                    traffic_used_gb=0.5,
                    device_limit=1,
                    autopay_enabled=False,
                ),
            ]
        )
        await db.commit()
        result = await create_paid_subscription(db, 1, 30, tariff_id=1, traffic_limit_gb=100, device_limit=5)
        saved = await observer.get(Subscription, 1)
        assert saved.is_trial is False and saved.autopay_enabled is True
        assert result._converted_from_trial is True


@pytest.mark.parametrize('same_tariff', [False, True])
async def test_saved_tariff_trial_purchase_rolls_back_without_refund(postgres_database, monkeypatch, same_tariff):
    from tests.database.test_multi_tariff_switch_postgres import switch_data

    async with switch_data(postgres_database, monkeypatch) as (db, user):
        sub = await db.get(Subscription, 11)
        sub.is_trial = True
        sub.tariff_id = 1 if same_tariff else None
        await db.commit()
        failure = AsyncMock(side_effect=RuntimeError('grant failed'))
        monkeypatch.setattr(
            'app.database.crud.subscription.' + ('extend_subscription' if same_tariff else 'create_paid_subscription'),
            failure,
        )
        monkeypatch.setattr(auto, '_reject_unavailable_saved_squads', AsyncMock(return_value=False))
        assert (
            await auto._auto_purchase_tariff(db, user, {'tariff_id': 1 if same_tariff else 3, 'period_days': 30})
            is False
        )
        failure.assert_awaited_once()
        await db.rollback()
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == 10000
        assert sub.is_trial is True
        assert list(await db.scalars(select(Transaction))) == []
