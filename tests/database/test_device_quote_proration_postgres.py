"""Real cabinet quotes must charge for the remaining period, including 14 days."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.cabinet.routes.subscription_modules.devices import get_device_price
from app.config import settings
from app.database.models import Base, Subscription, Tariff, Transaction, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('mode', ['classic', 'tariff', 'multi'])
@pytest.mark.parametrize('days', [1, 14, 30, 365])
async def test_actual_cabinet_device_quote_prorates_remaining_days(postgres_database, monkeypatch, mode, days):
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic' if mode == 'classic' else 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', mode == 'multi')
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 3000)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, status='active', language='ru', balance_kopeks=100000)
        db.add(user)
        if mode != 'classic':
            db.add(
                Tariff(
                    id=1,
                    name='Test',
                    device_limit=2,
                    max_device_limit=10,
                    device_price_kopeks=3000,
                    period_prices={'30': 10000},
                )
            )
        await db.flush()
        sub = Subscription(
            id=10,
            user_id=1,
            status='active',
            is_trial=False,
            device_limit=3,
            tariff_id=1 if mode != 'classic' else None,
            end_date=datetime.now(UTC) + timedelta(days=days),
        )
        db.add(sub)
        await db.commit()
        quote = await get_device_price(devices=1, subscription_id=10, user=user, db=db)
        assert quote['available'] is True
        assert quote['total_price_kopeks'] == days * 100
        await db.refresh(user)
        await db.refresh(sub)
        assert user.balance_kopeks == 100000 and sub.device_limit == 3
        assert list(await db.scalars(select(Transaction))) == []
