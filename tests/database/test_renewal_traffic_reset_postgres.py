from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.services import subscription_renewal_service as renewal
from app.services.pricing_engine import RenewalPricing
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('reset', [False, True])
@pytest.mark.parametrize('expired', [False, True])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('panel_exists', [False, True])
async def test_paid_renewal_resets_bot_and_panel_together(
    postgres_database, monkeypatch, reset, expired, multi, panel_exists
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'TRAFFIC_SELECTION_MODE', 'fixed_with_topup')
    monkeypatch.setattr(settings, 'FIXED_TRAFFIC_LIMIT_GB', 100)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
    monkeypatch.setattr(renewal, 'SubscriptionService', lambda: panel)
    now = datetime.now(UTC)
    old_end = now + timedelta(days=-1 if expired else 5)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(
            id=42,
            telegram_id=42,
            first_name='Test',
            language='ru',
            status='active',
            balance_kopeks=10000,
            remnawave_id=123 if panel_exists else None,
        )
        tariff = Tariff(
            id=1, name='Test', is_active=True, traffic_limit_gb=500, device_limit=2, period_prices={'30': 1000}
        )
        subscription = Subscription(
            id=7,
            user_id=42,
            tariff_id=1,
            is_trial=False,
            status='expired' if expired else 'active',
            start_date=now - timedelta(days=25),
            end_date=old_end,
            traffic_limit_gb=500,
            traffic_used_gb=17.5,
            device_limit=2,
            connected_squads=[],
            remnawave_id=123 if panel_exists else None,
        )
        db.add_all([user, tariff, subscription])
        await db.commit()
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
        result = await renewal.SubscriptionRenewalService().finalize(db, user, subscription, pricing)
        await db.refresh(user)
        await db.refresh(subscription)
        assert user.balance_kopeks == 9000
        assert subscription.traffic_used_gb == (0 if reset else 17.5)
        assert subscription.traffic_limit_gb == 500
        assert subscription.device_limit == 2
        expected_end = (now if expired else old_end) + timedelta(days=30)
        assert abs((subscription.end_date - expected_end).total_seconds()) < 3
        assert result.charged_from_balance_kopeks == 1000
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1 and transactions[0].amount_kopeks == -1000
        called = panel.update_remnawave_user if panel_exists else panel.create_remnawave_user
        other = panel.create_remnawave_user if panel_exists else panel.update_remnawave_user
        called.assert_awaited_once()
        other.assert_not_awaited()
        assert called.await_args.kwargs['reset_traffic'] is reset
