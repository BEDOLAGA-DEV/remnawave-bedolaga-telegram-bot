"""#3158: tariff quotas must not inherit classic fixed traffic settings."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.models import Base, Subscription, Tariff, TrafficPurchase, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('mode', ['fixed', 'fixed_with_topup', 'selectable'])
@pytest.mark.parametrize('reset', [False, True])
@pytest.mark.parametrize('expired', [False, True])
@pytest.mark.parametrize('base', [0, 500])
@pytest.mark.parametrize('explicit', [False, True])
async def test_tariff_renewal_preserves_base_and_only_active_purchases(
    postgres_database, monkeypatch, mode, reset, expired, base, explicit
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'TRAFFIC_SELECTION_MODE', mode)
    monkeypatch.setattr(settings, 'FIXED_TRAFFIC_LIMIT_GB', 100)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', reset)
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=42, telegram_id=42, first_name='Test', language='ru', status='active')
        tariff = Tariff(id=1, name='Test', traffic_limit_gb=base, device_limit=2, period_prices={'30': 1000})
        sub = Subscription(
            id=7,
            user_id=42,
            tariff_id=1,
            is_trial=False,
            status='expired' if expired else 'active',
            start_date=now - timedelta(days=30),
            end_date=now + timedelta(days=-1 if expired else 5),
            traffic_limit_gb=base + 30 if base else 0,
            purchased_traffic_gb=30,
            traffic_used_gb=17.5,
            device_limit=2,
            connected_squads=[],
        )
        db.add_all([user, tariff, sub])
        db.add_all(
            [
                TrafficPurchase(id=9, subscription_id=7, traffic_gb=20, expires_at=now + timedelta(days=60)),
                TrafficPurchase(id=10, subscription_id=7, traffic_gb=10, expires_at=now - timedelta(days=1)),
            ]
        )
        await db.commit()
        kwargs = {'traffic_limit_gb': base, 'reset_used_traffic': reset} if explicit else {}
        await extend_subscription(db, sub, 30, **kwargs)
        await db.refresh(sub)
        assert sub.traffic_limit_gb == (base + 20 if base else 0)
        # Existing unlimited policy removes now-redundant purchases.
        assert sub.purchased_traffic_gb == (20 if base else 0)
        assert sub.traffic_used_gb == (0 if reset else 17.5)
        purchases = (await db.scalars(select(TrafficPurchase))).all()
        assert [(p.id, p.traffic_gb) for p in purchases] == ([(9, 20)] if base else [])
        assert sub.tariff_id == 1
        assert sub.device_limit == 2
