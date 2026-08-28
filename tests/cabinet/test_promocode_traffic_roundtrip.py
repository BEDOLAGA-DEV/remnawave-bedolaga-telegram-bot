"""Трафик набора бонусов доживает до строки и обратно в кабинетном API.

Кабинет — единственная поверхность, где трафик сегодня можно завести
(в Telegram-админке поля нет), поэтому её маршруты стоит держать под
сквозным тестом, а не только валидацию.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes.admin_promocodes import (
    PromoCodeCreateRequest,
    PromoCodeUpdateRequest,
    create_promocode_endpoint,
    update_promocode_endpoint,
)
from app.database.models import PromoCode, PromoCodeType, PromoCodeUse, User
from tests.fixtures.sqlite_memory import memory_session


# Загрузчики промокода подтягивают связь uses — без таблицы падает SELECT.
TABLES = (User.__table__, PromoCode.__table__, PromoCodeUse.__table__)


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='admin')


def _create(**overrides) -> PromoCodeCreateRequest:
    base = dict(
        code='COMBO',
        type=PromoCodeType.BALANCE_AND_DAYS,
        balance_bonus_kopeks=10000,
        subscription_days=7,
        traffic_gb=0,
        max_uses=1,
    )
    base.update(overrides)
    return PromoCodeCreateRequest(**base)


@pytest.mark.asyncio
async def test_traffic_survives_create(monkeypatch):
    """Указанный при создании трафик попадает в строку и в ответ."""
    async with memory_session(monkeypatch, TABLES) as db:
        created = await create_promocode_endpoint(_create(traffic_gb=50), admin=_admin(), db=db)

        assert created.traffic_gb == 50
        stored = await db.get(PromoCode, created.id)
        assert stored.traffic_gb == 50


@pytest.mark.asyncio
async def test_traffic_is_updatable(monkeypatch):
    """Правка меняет трафик, а не отвечает 200 со старым значением."""
    async with memory_session(monkeypatch, TABLES) as db:
        created = await create_promocode_endpoint(_create(traffic_gb=10), admin=_admin(), db=db)

        updated = await update_promocode_endpoint(
            created.id, PromoCodeUpdateRequest(traffic_gb=100), admin=_admin(), db=db
        )

        assert updated.traffic_gb == 100
        stored = await db.get(PromoCode, created.id)
        assert stored.traffic_gb == 100
