"""Причина «докупить/уменьшить устройства нельзя» — машинный код, а не текст.

/devices/price и /devices/reduction-info отдавали готовую фразу в ``reason``: одна
на русском, другая на английском, и кабинет показывал её как есть — человек видел
«Already at minimum device limit» посреди русского интерфейса. Теперь рядом с
текстом (он остаётся для старых кабинетов) идёт ``reason_code``, а перевод берёт
кабинет из своей локали.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


DEVICES_FILE = (
    Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'subscription_modules' / 'devices.py'
)
TABLES = list(Base.metadata.sorted_tables)


def _user() -> User:
    return User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0)


def _tariff(
    *, device_limit: int = 1, device_price_kopeks: int | None = 100, max_device_limit: int | None = 3
) -> Tariff:
    return Tariff(
        id=1,
        name='Pro',
        description='',
        is_active=True,
        traffic_limit_gb=100,
        device_limit=device_limit,
        device_price_kopeks=device_price_kopeks,
        max_device_limit=max_device_limit,
        allowed_squads=[],
        display_order=1,
    )


def _subscription(*, status: str = 'active', is_trial: bool = False, device_limit: int = 1) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=10,
        remnawave_short_id='dev1',
        user_id=1,
        status=status,
        is_trial=is_trial,
        start_date=now,
        end_date=now + timedelta(days=10),
        traffic_limit_gb=100,
        device_limit=device_limit,
        tariff_id=1,
        connected_squads=[],
    )


@pytest.fixture(autouse=True)
def multi_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', False)


async def _seed(db, *rows):
    db.add_all(rows)
    await db.commit()


class TestReductionInfo:
    @pytest.mark.asyncio
    async def test_no_subscription(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.devices import get_device_reduction_info

        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, _user())
            info = await get_device_reduction_info(subscription_id=None, user=await db.get(User, 1), db=db)
        assert info['available'] is False
        assert info['reason_code'] == 'no_subscription'

    @pytest.mark.asyncio
    async def test_trial(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.devices import get_device_reduction_info

        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, _user(), _tariff(), _subscription(status=SubscriptionStatus.TRIAL.value, is_trial=True))
            info = await get_device_reduction_info(subscription_id=10, user=await db.get(User, 1), db=db)
        assert info['reason_code'] == 'trial'

    @pytest.mark.asyncio
    async def test_at_minimum_keeps_text_and_adds_code(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.devices import get_device_reduction_info

        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, _user(), _tariff(device_limit=1), _subscription(device_limit=1))
            info = await get_device_reduction_info(subscription_id=10, user=await db.get(User, 1), db=db)
        assert info['available'] is False
        assert info['reason_code'] == 'at_minimum'
        assert info['min_device_limit'] == 1
        assert info['reason'], 'текст остаётся для кабинетов, которые кода ещё не знают'


class TestDevicePrice:
    @pytest.mark.asyncio
    async def test_no_active_subscription(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.devices import get_device_price

        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, _user(), _tariff(), _subscription(status=SubscriptionStatus.EXPIRED.value))
            info = await get_device_price(devices=1, subscription_id=10, user=await db.get(User, 1), db=db)
        assert info['reason_code'] == 'no_active_subscription'

    @pytest.mark.asyncio
    async def test_devices_unavailable_when_tariff_has_no_price(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.devices import get_device_price

        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, _user(), _tariff(device_price_kopeks=0), _subscription())
            info = await get_device_price(devices=1, subscription_id=10, user=await db.get(User, 1), db=db)
        assert info['reason_code'] == 'devices_unavailable'

    @pytest.mark.asyncio
    async def test_max_devices_reached(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.devices import get_device_price

        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, _user(), _tariff(max_device_limit=2), _subscription(device_limit=2))
            info = await get_device_price(devices=1, subscription_id=10, user=await db.get(User, 1), db=db)
        assert info['reason_code'] == 'max_devices_reached'
        assert info['max_device_limit'] == 2

    @pytest.mark.asyncio
    async def test_can_add_limited(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.devices import get_device_price

        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, _user(), _tariff(max_device_limit=3), _subscription(device_limit=1))
            info = await get_device_price(devices=5, subscription_id=10, user=await db.get(User, 1), db=db)
        assert info['reason_code'] == 'can_add_limited'
        assert info['can_add'] == 2


def test_every_unavailable_answer_carries_a_code():
    """Сторож: новый отказ без кода снова покажет человеку фразу на чужом языке."""
    lines = DEVICES_FILE.read_text(encoding='utf-8').splitlines()
    refusals = [i for i, line in enumerate(lines) if "'available': False" in line]
    assert len(refusals) >= 7, 'ожидали все ветки отказа двух эндпоинтов'
    without_code = [
        f'{DEVICES_FILE.name}:{i + 1}'
        for i in refusals
        if not any("'reason_code'" in line for line in lines[i + 1 : i + 4])
    ]
    assert without_code == [], without_code
