from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.crud.user import lock_user_for_pricing
from app.database.models import Base, Subscription, Tariff, User
from app.handlers.subscription import tariff_purchase as m
from app.utils.formatting import format_price_kopeks
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.parametrize(
    ('entry', 'expired', 'multi'),
    [
        (entry, expired, multi)
        for entry in ['select', 'days', 'traffic', 'period']
        for expired in [False, True]
        for multi in [False, True]
        if not (entry == 'select' and multi and not expired)
    ],
)
@pytest.mark.parametrize('base_limit', [0, 2])
async def test_custom_preview_shows_inherited_devices_and_checkout_price(
    monkeypatch, entry, expired, multi, base_limit
):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                User(id=42, telegram_id=42, first_name='Test', language='ru', status='active', balance_kopeks=10000),
                Tariff(
                    id=1,
                    name='Test',
                    is_active=True,
                    traffic_limit_gb=100,
                    device_limit=base_limit,
                    device_price_kopeks=100,
                    period_prices={'30': 300},
                    custom_days_enabled=True,
                    price_per_day_kopeks=10,
                    min_days=30,
                    max_days=365,
                    custom_traffic_enabled=True,
                    traffic_price_per_gb_kopeks=1,
                    min_traffic_gb=0,
                    max_traffic_gb=1000,
                    allowed_squads=['test-squad'],
                ),
                Subscription(
                    id=7,
                    user_id=42,
                    tariff_id=1,
                    is_trial=False,
                    status='expired' if expired else 'active',
                    start_date=now - timedelta(days=25),
                    end_date=now + timedelta(days=-1 if expired else 5),
                    traffic_limit_gb=100,
                    device_limit=5,
                    connected_squads=['test-squad'],
                ),
            ]
        )
        await db.commit()
        user = await lock_user_for_pricing(db, 42)
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={'custom_days': 30, 'custom_traffic_gb': 0}),
            update_data=AsyncMock(),
            set_state=AsyncMock(),
        )
        data, handler = {
            'select': ('tariff:1', m.select_tariff),
            'days': ('custom_days:1:0', m.handle_custom_days_change),
            'traffic': ('custom_traffic:1:0', m.handle_custom_traffic_change),
            'period': ('period_traffic:1:30', m.select_tariff_period_with_traffic),
        }[entry]
        callback = SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        await handler(callback, user, db, state)
        rendered = callback.message.edit_text.await_args.args[0]
        assert f'Итого: {format_price_kopeks(300 if base_limit == 0 else 600)}' in rendered
        assert f'Устройств: {m.Texts.format_device_limit(0 if base_limit == 0 else 5)}' in rendered
