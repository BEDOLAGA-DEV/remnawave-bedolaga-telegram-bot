from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, Tariff, User
from app.handlers.subscription import tariff_purchase as telegram
from app.services import subscription_auto_purchase_service as auto
from app.services.user_cart_service import user_cart_service
from app.webapi.routes import miniapp
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


class PanelBoundary(BaseException):
    """Inspect persisted state before external panel I/O."""


@pytest.mark.parametrize('flow', ['telegram', 'custom', 'miniapp', 'auto'])
@pytest.mark.parametrize('reset', [False, True])
@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('trial', [False, True])
async def test_tariff_purchase_uses_same_reset_decision_in_db_and_panel(
    postgres_database, monkeypatch, flow, reset, multi, trial
):
    from app.services import subscription_renewal_service as renewal

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_TARIFF_SWITCH', False)
    monkeypatch.setattr(renewal, 'with_admin_notification_service', AsyncMock())
    monkeypatch.setattr(user_cart_service, 'save_user_cart', AsyncMock())
    panel_call = AsyncMock(side_effect=PanelBoundary)
    panel = SimpleNamespace(
        create_remnawave_user=panel_call, update_remnawave_user=panel_call, sync_remnawave_user=panel_call
    )
    module = telegram if flow in {'telegram', 'custom'} else miniapp if flow == 'miniapp' else auto
    monkeypatch.setattr(module, 'SubscriptionService', lambda: panel)
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
        tariff = Tariff(
            id=1,
            name='Test',
            is_active=True,
            is_daily=False,
            traffic_limit_gb=100,
            device_limit=2,
            period_prices={'30': 300},
            allowed_squads=['test-squad'],
            custom_days_enabled=True,
            price_per_day_kopeks=10,
        )
        subscription = Subscription(
            id=7,
            user_id=42,
            tariff_id=1,
            is_trial=trial,
            status='active',
            start_date=now - timedelta(days=25),
            end_date=now + timedelta(days=5),
            traffic_limit_gb=100,
            traffic_used_gb=17.5,
            device_limit=2,
            connected_squads=['test-squad'],
            remnawave_id=123,
        )
        db.add_all([user, tariff, subscription])
        await db.commit()
        if flow in {'telegram', 'custom'}:
            callback = SimpleNamespace(
                data='tariff_confirm:1:30', answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock())
            )
            state = SimpleNamespace(
                get_data=AsyncMock(
                    return_value={'target_subscription_id': 7, 'custom_days': 30, 'custom_traffic_gb': 100}
                )
            )
            handler = telegram.handle_custom_confirm if flow == 'custom' else telegram.confirm_tariff_purchase
            operation = handler(callback, user, db, state)
        elif flow == 'miniapp':
            monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
            payload = miniapp.MiniAppTariffPurchaseRequest(initData='test', tariffId=1, periodDays=30)
            operation = miniapp.purchase_tariff_endpoint(payload, db)
        else:
            operation = auto._auto_purchase_tariff(
                db, user, {'tariff_id': 1, 'period_days': 30, 'total_price': 300, 'subscription_id': 7}
            )
        with pytest.raises(PanelBoundary):
            await operation
        await db.refresh(subscription)
        await db.refresh(user)
        expected_reset = reset or trial
        assert subscription.traffic_used_gb == (0 if expected_reset else 17.5)
        assert panel_call.await_args.kwargs['reset_traffic'] is expected_reset
        assert user.balance_kopeks == 9700
        assert subscription.tariff_id == 1
        assert subscription.is_trial is False
