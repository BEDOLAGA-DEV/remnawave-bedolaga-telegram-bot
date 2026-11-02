"""Saved traffic carts may not guess a target or bypass disabled tariff top-ups."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    'case', ['pinned', 'single_legacy', 'no_autopay', 'one_autopay', 'two_autopay', 'topup_disabled', 'package_removed']
)
async def test_saved_traffic_respects_selection_and_tariff(postgres_database, monkeypatch, case):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(type(settings), 'get_traffic_topup_price', lambda self, gb: 1000)
    monkeypatch.setattr(auto, '_delete_cart_for_subscription', AsyncMock())
    sync = AsyncMock()
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', sync)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, status='active', language='ru', balance_kopeks=5000)
        db.add(user)
        for tid in [1, 2]:
            db.add(
                Tariff(
                    id=tid,
                    name=f'Test-{tid}',
                    traffic_limit_gb=100,
                    traffic_topup_enabled=case != 'topup_disabled',
                    traffic_topup_packages={} if case == 'package_removed' else {'10': 1000},
                )
            )
        await db.flush()
        for sid, tid in [(10, 1)] if case == 'single_legacy' else [(10, 1), (20, 2)]:
            db.add(
                Subscription(
                    id=sid,
                    user_id=1,
                    tariff_id=tid,
                    status='active',
                    is_trial=False,
                    traffic_limit_gb=100,
                    device_limit=3,
                    remnawave_short_id=f'masked-{sid}',
                    autopay_enabled=case != 'no_autopay' and (sid == 20 or case == 'two_autopay'),
                    end_date=datetime.now(UTC) + timedelta(days=30),
                )
            )
        await db.commit()
        cart = {'cart_mode': 'add_traffic', 'traffic_gb': 10, 'price_kopeks': 1000}
        if case not in ['single_legacy', 'no_autopay', 'one_autopay', 'two_autopay']:
            cart['subscription_id'] = 10
        result = await auto._auto_add_traffic(db, user, cart)
        assert result is (case in ['pinned', 'single_legacy'])
        await db.refresh(user)
        subs = list(await db.scalars(select(Subscription).order_by(Subscription.id)))
        if case in ['pinned', 'single_legacy']:
            assert user.balance_kopeks == 4000
            assert [s.traffic_limit_gb for s in subs] == ([110] if case == 'single_legacy' else [110, 100])
        else:
            assert user.balance_kopeks == 5000
            assert [s.traffic_limit_gb for s in subs] == [100, 100]
            assert list(await db.scalars(select(Transaction))) == []
            sync.assert_not_awaited()
