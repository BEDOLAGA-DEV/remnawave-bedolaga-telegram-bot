from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, TrafficPurchase, User
from app.handlers import simple_subscription as simple
from app.services import subscription_service
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class PanelBoundary(BaseException):
    pass


@pytest.mark.parametrize(
    ('flow', 'trial', 'expired'),
    [
        ('confirm', False, False),
        ('confirm', False, True),
        ('direct', False, True),
        ('confirm', True, False),
        ('direct', True, False),
        ('confirm', True, True),
        ('direct', True, True),
    ],
)
@pytest.mark.parametrize('reset', [False, True])
async def test_simple_payment_resets_db_and_panel_together(postgres_database, monkeypatch, flow, trial, expired, reset):
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'SIMPLE_SUBSCRIPTION_ENABLED', True)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset)
    monkeypatch.setattr(simple, '_ensure_simple_subscription_squad_uuid', AsyncMock(return_value='test-squad'))
    monkeypatch.setattr(simple, '_calculate_simple_subscription_price', AsyncMock(return_value=(300, {})))
    panel_call = AsyncMock(side_effect=PanelBoundary)
    monkeypatch.setattr(
        subscription_service, 'SubscriptionService', type('Panel', (), {'create_remnawave_user': panel_call})
    )
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(
            id=42,
            telegram_id=42,
            first_name='Test',
            language='ru',
            status='active',
            balance_kopeks=10000,
            remnawave_id=123,
        )
        sub = Subscription(
            id=7,
            user_id=42,
            is_trial=trial,
            status='expired' if expired else 'active',
            start_date=now - timedelta(days=25),
            end_date=now + timedelta(days=-1 if expired else 5),
            traffic_limit_gb=120,
            purchased_traffic_gb=20,
            traffic_used_gb=17.5,
            device_limit=2,
            connected_squads=['test-squad'],
            remnawave_id=123,
        )
        db.add_all([user, sub])
        await db.commit()
        db.add(TrafficPurchase(id=9, subscription_id=7, traffic_gb=20, expires_at=now + timedelta(days=60)))
        await db.commit()
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    'subscription_params': {
                        'period_days': 30,
                        'traffic_limit_gb': 100,
                        'device_limit': 2,
                        'squad_uuid': 'test-squad',
                    }
                }
            )
        )
        callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        handler = (
            simple.confirm_simple_subscription_purchase
            if flow == 'confirm'
            else simple.handle_simple_subscription_pay_with_balance
        )
        with pytest.raises(PanelBoundary):
            await handler(callback, user, state, db)
        await db.refresh(sub)
        await db.refresh(user)
        assert sub.traffic_used_gb == (0 if reset or trial else 17.5)
        assert panel_call.await_args.kwargs.get('reset_traffic', False) is (reset or trial)
        assert user.balance_kopeks == 9700
        assert sub.is_trial is False
        assert sub.traffic_limit_gb == 120
        assert await db.get(TrafficPurchase, 9) is not None
