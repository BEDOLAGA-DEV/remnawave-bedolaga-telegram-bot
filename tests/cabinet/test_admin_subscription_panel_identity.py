"""TG-113: support identity belongs to each subscription in the admin response."""

from datetime import UTC, datetime, timedelta

import pytest

from app.cabinet.routes.admin_users import _build_subscription_info_async
from app.config import settings
from app.database.models import Subscription, TrafficPurchase, User
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.parametrize('panel_id', [501, 502, None])
async def test_admin_subscription_response_preserves_own_panel_identity(monkeypatch, panel_id):
    monkeypatch.setattr(settings, 'PLATEGA_RECURRENT_ENABLED', False)
    async with memory_session(monkeypatch, (User.__table__, Subscription.__table__, TrafficPurchase.__table__)) as db:
        user = User(telegram_id=1001, remnawave_id=999, balance_kopeks=0)
        db.add(user)
        await db.flush()
        sub = Subscription(
            user_id=user.id,
            remnawave_id=panel_id,
            status='active',
            is_trial=False,
            start_date=datetime.now(UTC),
            end_date=datetime.now(UTC) + timedelta(days=10),
            traffic_limit_gb=100,
            device_limit=3,
            autopay_enabled=False,
        )
        db.add(sub)
        await db.commit()

        payload = (await _build_subscription_info_async(db, sub)).model_dump(mode='json')

        assert 'remnawave_id' in payload
        assert payload['remnawave_id'] == panel_id
        assert payload['id'] == sub.id
