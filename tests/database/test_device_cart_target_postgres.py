"""The cabinet's saved device checkout must retain the selected subscription."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import devices
from app.cabinet.schemas.subscription import DevicePurchaseRequest
from app.config import settings
from app.database.models import Base, Subscription, Tariff, Transaction, User
from app.handlers.subscription import devices as telegram_devices
from app.services import subscription_auto_purchase_service as auto
from app.services.subscription_service import SubscriptionService
from tests.fixtures.postgres_db import postgres_session
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401 - shared real Redis fixture


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('entry', ['cart', 'purchase', 'legacy', 'telegram_add', 'telegram_change'])
@pytest.mark.parametrize('target_state', ['present', 'deleted', 'foreign', 'legacy_unpinned'])
async def test_device_topup_keeps_selected_subscription(postgres_database, monkeypatch, carts, entry, target_state):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    service, uid, _ = carts
    monkeypatch.setattr(devices, 'user_cart_service', service)
    monkeypatch.setattr(telegram_devices, 'user_cart_service', service)
    sync = AsyncMock()
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', sync)
    now = datetime.now(UTC)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=uid, status='active', language='ru', balance_kopeks=0)
        db.add_all([user, User(id=uid + 1, status='active')])
        for tid in [1, 2]:
            db.add(
                Tariff(
                    id=tid,
                    name=f'Test-{tid}',
                    device_limit=2,
                    device_price_kopeks=1000,
                    max_device_limit=20,
                    period_prices={'30': 10000},
                )
            )
        await db.flush()
        for sid, tid, limit, created in [(10, 1, 3, now - timedelta(days=2)), (20, 2, 7, now - timedelta(days=1))]:
            db.add(
                Subscription(
                    id=sid,
                    user_id=uid,
                    tariff_id=tid,
                    status='active',
                    is_trial=False,
                    device_limit=limit,
                    remnawave_short_id=f'masked-{sid}',
                    end_date=now + timedelta(days=30),
                    created_at=created,
                )
            )
        await db.commit()
        if entry.startswith('telegram'):
            selected = await db.get(Subscription, 10)
            monkeypatch.setattr(telegram_devices, '_resolve_subscription', AsyncMock(return_value=(selected, 10)))
            callback = SimpleNamespace(
                data='add_devices_1' if entry == 'telegram_add' else 'change_devices_4',
                answer=AsyncMock(),
                message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
            )
            handler = (
                telegram_devices.confirm_add_devices
                if entry == 'telegram_add'
                else telegram_devices.confirm_change_devices
            )
            await handler(callback, user, db)
        else:
            handler = {
                'cart': devices.save_devices_cart,
                'purchase': devices.purchase_devices,
                'legacy': devices.purchase_devices_legacy,
            }[entry]
            if entry == 'cart':
                await handler(DevicePurchaseRequest(devices=1), subscription_id=10, user=user, db=db)
            else:
                with pytest.raises(HTTPException) as error:
                    await handler(DevicePurchaseRequest(devices=1), subscription_id=10, user=user, db=db)
                assert error.value.status_code == 402
        cart = await service.get_user_cart(uid)
        if target_state == 'legacy_unpinned':
            await service.delete_user_cart(uid)
            unpinned = dict(cart)
            unpinned.pop('subscription_id', None)
            await service.save_user_cart(uid, unpinned)
        selected = await db.get(Subscription, 10)
        if target_state == 'deleted':
            await db.delete(selected)
        elif target_state == 'foreign':
            selected.user_id = uid + 1
        user.balance_kopeks = 5000
        await db.commit()
        bought = await auto.auto_purchase_saved_cart_after_topup(db, user)
        assert bought is (target_state == 'present')
        await db.refresh(user)
        neighbor = await db.get(Subscription, 20, populate_existing=True)
        assert neighbor.device_limit == 7
        if target_state == 'present':
            await db.refresh(selected)
            assert selected.device_limit == 4
            assert user.balance_kopeks == 4000
            sync.assert_awaited_once()
            assert await service.get_subscription_cart(uid, 10) is None
        else:
            assert user.balance_kopeks == 5000
            assert list(await db.scalars(select(Transaction))) == []
            sync.assert_not_awaited()
        assert cart['subscription_id'] == 10
