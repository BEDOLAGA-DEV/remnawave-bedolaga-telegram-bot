"""Preview and checkout must find an expired same-tariff trial in place."""

from datetime import UTC, datetime, timedelta

import pytest

from app.cabinet.routes.subscription_modules.purchase import _resolve_tariff_purchase_target
from app.cabinet.schemas.subscription import TariffPurchasePreviewRequest, TariffPurchaseRequest
from app.config import settings
from app.database.models import Base, Subscription, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.parametrize('request_class', [TariffPurchasePreviewRequest, TariffPurchaseRequest])
@pytest.mark.parametrize('trial_status', ['expired', 'disabled'])
async def test_purchase_tariff_tariff_lookup_includes_inactive(monkeypatch, request_class, trial_status):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, status='active', balance_kopeks=0)
        tariff = Tariff(id=7, name='Test', is_active=True, device_limit=1, period_prices={'30': 30000})
        trial = Subscription(
            id=10,
            user_id=1,
            tariff_id=7,
            is_trial=True,
            status=trial_status,
            device_limit=1,
            end_date=datetime.now(UTC) - timedelta(days=1),
        )
        db.add_all([user, tariff, trial])
        await db.commit()
        target = await _resolve_tariff_purchase_target(db, user, tariff, request_class(tariff_id=7, period_days=30))
        assert target is not None and target.id == 10 and target.is_trial
