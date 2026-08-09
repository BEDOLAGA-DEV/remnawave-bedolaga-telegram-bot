"""Regression tests for preserving devices on classic -> tariff migration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.database.crud.subscription import resolve_tariff_purchase_device_limit
from app.database.models import (
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
    tariff_promo_groups,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
)


def test_legacy_device_limit_is_preserved_within_tariff_maximum(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    tariff = SimpleNamespace(id=7, device_limit=1, max_device_limit=5)
    legacy_subscription = SimpleNamespace(tariff_id=None, device_limit=3)

    assert resolve_tariff_purchase_device_limit(legacy_subscription, tariff) == 3


def test_legacy_device_limit_is_capped_by_tariff_maximum(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    tariff = SimpleNamespace(id=7, device_limit=1, max_device_limit=2)
    legacy_subscription = SimpleNamespace(tariff_id=None, device_limit=3)

    assert resolve_tariff_purchase_device_limit(legacy_subscription, tariff) == 2


def test_real_tariff_switch_still_resets_devices_to_new_base(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    tariff = SimpleNamespace(id=7, device_limit=1, max_device_limit=5)
    another_tariff_subscription = SimpleNamespace(tariff_id=6, device_limit=3)

    assert resolve_tariff_purchase_device_limit(another_tariff_subscription, tariff) == 1


async def test_legacy_preview_prices_and_saves_preserved_devices(monkeypatch) -> None:
    from app.handlers.subscription import tariff_purchase as handler

    async with memory_session(monkeypatch, TABLES) as db:
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
        monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
        monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
        monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 5000)

        user = User(
            telegram_id=501,
            username='legacy501',
            first_name='Legacy',
            status=UserStatus.ACTIVE.value,
            language='ru',
            balance_kopeks=10000,
        )
        db.add(user)
        await db.commit()

        tariff = Tariff(
            name='Новый тариф',
            is_active=True,
            device_limit=1,
            max_device_limit=5,
            traffic_limit_gb=100,
            period_prices={'30': 10000},
        )
        db.add(tariff)
        await db.commit()

        now = datetime.now(UTC)
        db.add(
            Subscription(
                user_id=user.id,
                tariff_id=None,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=20),
                end_date=now + timedelta(days=10),
                device_limit=3,
                remnawave_short_id='legacy-devices',
            )
        )
        await db.commit()

        save_cart = AsyncMock()
        monkeypatch.setattr(handler.user_cart_service, 'save_user_cart', save_cart)

        callback = SimpleNamespace(
            data=f'tariff_period:{tariff.id}:30',
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        state = MagicMock()
        state.update_data = AsyncMock()

        await handler.select_tariff_period.__wrapped__(callback, user, db, state)

        rendered = callback.message.edit_text.await_args.args[0]
        assert 'Недостаточно средств' in rendered
        assert '200' in rendered  # 100 ₽ tariff + 2 × 50 ₽ extra devices

        saved_cart = save_cart.await_args.args[1]
        assert saved_cart['device_limit'] == 3
        assert saved_cart['total_price'] == 20000
