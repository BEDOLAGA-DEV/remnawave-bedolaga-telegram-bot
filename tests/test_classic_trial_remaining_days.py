from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import settings
from app.handlers.admin import users as admin_users
from app.handlers.subscription import purchase as telegram_purchase
from app.services import subscription_purchase_service as miniapp_purchase


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


class PurchasePersisted(BaseException):
    """Stop at the persistence boundary, before external side effects."""


@pytest.mark.parametrize('flow', ['miniapp', 'telegram', 'admin'])
@pytest.mark.parametrize(('carry', 'reset'), [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize(
    ('trial', 'remaining'),
    [
        (True, timedelta(days=4, hours=19, minutes=12)),
        (True, timedelta()),
        (True, -timedelta(days=1)),
        (False, timedelta(days=5)),
        (False, timedelta()),
        (False, -timedelta(days=1)),
        (True, None),
        (False, None),
    ],
)
async def test_classic_purchase_counts_trial_remainder_once(monkeypatch, flow, carry, reset, trial, remaining):
    """All three actual purchase flows must persist the same hand-calculated expiry."""
    from app.database.crud import subscription_conversion, user as user_crud
    from app.services import grace_access_echo

    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', carry)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', reset)
    monkeypatch.setattr(grace_access_echo, 'undo_grace_overlay_echo', AsyncMock())
    monkeypatch.setattr(subscription_conversion, 'create_subscription_conversion', AsyncMock())
    subscription = SimpleNamespace(
        id=7,
        user_id=42,
        is_trial=trial,
        start_date=NOW - timedelta(days=2),
        end_date=NOW + remaining if remaining is not None else None,
        status='active',
        is_active=True,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['test-squad'],
    )
    user = SimpleNamespace(id=42, language='ru', telegram_id=42, balance_kopeks=100000, subscription=subscription)
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(user_crud, 'subtract_user_balance', AsyncMock(return_value=True))
    db = AsyncMock()
    db.commit.side_effect = PurchasePersisted
    callback = SimpleNamespace(
        data='admin_buy_sub_execute_42_30_1000', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    if flow == 'miniapp':
        m = miniapp_purchase

        monkeypatch.setattr(m, 'datetime', FrozenDateTime)
        monkeypatch.setattr(m, 'subtract_user_balance', AsyncMock(return_value=True))
        monkeypatch.setattr(m, 'create_subscription_conversion', AsyncMock())
        monkeypatch.setattr(m, 'create_transaction', AsyncMock())
        db.execute.return_value = SimpleNamespace(
            scalar_one_or_none=lambda: subscription, scalars=lambda: SimpleNamespace(all=list)
        )
        context = SimpleNamespace(user=user, subscription=subscription)
        pricing = SimpleNamespace(
            final_total=1000,
            base_original_total=1000,
            promo_discount_value=0,
            months=1,
            selection=SimpleNamespace(
                period=SimpleNamespace(days=30), traffic_value=100, devices=2, servers=['test-squad']
            ),
        )
        operation = m.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
    elif flow == 'telegram':
        m = telegram_purchase
        from app.services.pricing_engine import PricingEngine, pricing_engine

        monkeypatch.setattr(m, 'datetime', FrozenDateTime)
        monkeypatch.setattr(m, 'subtract_user_balance', AsyncMock(return_value=True))
        monkeypatch.setattr(m, 'save_subscription_checkout_draft', AsyncMock())
        monkeypatch.setattr(m, 'should_offer_checkout_resume', lambda *args: False)
        price = SimpleNamespace(final_total=1000, promo_offer_discount=0, breakdown={})
        monkeypatch.setattr(pricing_engine, 'calculate_classic_new_subscription_price', AsyncMock(return_value=price))
        details = defaultdict(
            int, servers_individual_prices=[], months_in_period=1, base_price_original=1000, base_price=1000
        )
        monkeypatch.setattr(PricingEngine, 'classic_pricing_to_purchase_details', Mock(return_value=details))
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={'period_days': 30, 'total_price': 1000, 'devices': 2, 'countries': ['test-squad']}
            )
        )
        operation = m.confirm_purchase(callback, state, user, db)
    else:
        m = admin_users

        monkeypatch.setattr(m, 'datetime', FrozenDateTime)
        monkeypatch.setattr(
            m.UserService, 'get_user_profile', AsyncMock(return_value={'user': user, 'subscription': subscription})
        )
        monkeypatch.setattr(m, '_calculate_subscription_period_price', AsyncMock(return_value=1000))
        handler = m.admin_buy_subscription_execute
        while hasattr(handler, '__wrapped__'):
            handler = handler.__wrapped__
        operation = handler(callback, user, db)
    with pytest.raises(PurchasePersisted):
        await operation
    expected_remaining = (
        max(remaining or timedelta(), timedelta()) if not trial or (carry and not reset) else timedelta()
    )
    assert subscription.end_date == NOW + timedelta(days=30) + expected_remaining
    assert subscription.pending_panel_expire_at == subscription.end_date
    assert subscription.is_trial is False


@pytest.mark.postgres
@pytest.mark.parametrize(('carry', 'reset'), [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('trial', [False, True])
async def test_classic_purchase_persists_expiry_balance_and_conversion(
    postgres_database, monkeypatch, carry, reset, trial
):
    from sqlalchemy import func, select

    from app.database.models import Base, Subscription, SubscriptionConversion, Transaction, User
    from app.services import subscription_purchase_service as m
    from tests.fixtures.postgres_db import postgres_session

    monkeypatch.setattr(settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', carry)
    monkeypatch.setattr(settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', reset)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(m, 'datetime', FrozenDateTime)
    panel = SimpleNamespace(create_remnawave_user=AsyncMock(), update_remnawave_user=AsyncMock())
    monkeypatch.setattr(m, 'SubscriptionService', lambda: panel)
    remaining = timedelta(days=4, hours=19, minutes=12)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=42, telegram_id=42, first_name='Test', language='ru', status='active', balance_kopeks=100000)
        subscription = Subscription(
            id=7,
            user_id=42,
            is_trial=trial,
            status='active',
            start_date=NOW - timedelta(days=2),
            end_date=NOW + remaining,
            traffic_limit_gb=100,
            device_limit=2,
            connected_squads=[],
        )
        db.add_all([user, subscription])
        await db.commit()
        context = SimpleNamespace(user=user, subscription=subscription)
        pricing = SimpleNamespace(
            final_total=1000,
            base_original_total=1000,
            promo_discount_value=0,
            server_ids=[],
            months=1,
            selection=SimpleNamespace(period=SimpleNamespace(days=30), traffic_value=100, devices=2, servers=[]),
        )
        result = await m.MiniAppSubscriptionPurchaseService().submit_purchase(db, context, pricing)
        await db.refresh(subscription)
        await db.refresh(user)
        expected_remaining = remaining if not trial or (carry and not reset) else timedelta()
        assert subscription.end_date == NOW + timedelta(days=30) + expected_remaining
        assert subscription.pending_panel_expire_at == subscription.end_date
        assert subscription.is_trial is False
        assert user.balance_kopeks == 99000
        assert result['was_trial_conversion'] is trial
        assert await db.scalar(select(func.count()).select_from(SubscriptionConversion)) == int(trial)
        transactions = (await db.scalars(select(Transaction))).all()
        assert len(transactions) == 1
        assert transactions[0].amount_kopeks == -1000
