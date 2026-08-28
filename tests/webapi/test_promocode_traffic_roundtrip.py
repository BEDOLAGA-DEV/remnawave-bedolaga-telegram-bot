"""Трафик набора бонусов должен доживать до строки и обратно во внешнем API.

Схемы ``/promo-codes`` объявляют ``traffic_gb`` на создании, чтении и правке,
а валидация создания засчитывает его как «хотя бы одна составляющая набора».
Если при этом поле не доезжает до строки, получается худший из исходов: код
с одним лишь трафиком проходит валидацию, создаётся пустым и на активации
сжигает попытку пользователя, отрапортовав об успехе.

Тесты гоняют настоящие корутины маршрутов поверх :memory: БД — моков между
запросом и строкой нет.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.database.models import PromoCode, PromoCodeType, PromoCodeUse, User
from app.webapi.routes.promocodes import (
    create_promocode_endpoint,
    get_promocode,
    update_promocode_endpoint,
)
from app.webapi.schemas.promocodes import PromoCodeCreateRequest, PromoCodeUpdateRequest
from tests.fixtures.sqlite_memory import memory_session


# get_promocode_by_code/_by_id подтягивают связь uses — без таблицы падает SELECT.
TABLES = (User.__table__, PromoCode.__table__, PromoCodeUse.__table__)


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
async def test_traffic_survives_create_and_read_back(monkeypatch):
    """Созданный через API код хранит трафик и отдаёт его обратно."""
    async with memory_session(monkeypatch, TABLES) as db:
        created = await create_promocode_endpoint(_create(traffic_gb=50), db=db)
        assert created.traffic_gb == 50

        stored = await db.get(PromoCode, created.id)
        assert stored.traffic_gb == 50

        # Чтение отдельным маршрутом: сериализатор — общий, но лгать он может тихо
        detail = await get_promocode(created.id, db=db)
        assert detail.traffic_gb == 50


@pytest.mark.asyncio
async def test_traffic_is_updatable(monkeypatch):
    """PATCH меняет трафик, а не молча отвечает 200 со старым значением."""
    async with memory_session(monkeypatch, TABLES) as db:
        created = await create_promocode_endpoint(_create(traffic_gb=10), db=db)

        updated = await update_promocode_endpoint(created.id, PromoCodeUpdateRequest(traffic_gb=100), db=db)

        assert updated.traffic_gb == 100
        stored = await db.get(PromoCode, created.id)
        assert stored.traffic_gb == 100


@pytest.mark.asyncio
async def test_traffic_only_set_is_not_created_empty(monkeypatch):
    """Набор из одного трафика создаётся именно трафиком, а не пустышкой.

    Валидация засчитывает трафик как составляющую — значит, до строки он
    обязан доехать, иначе код проходит проверку и не даёт ничего.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        created = await create_promocode_endpoint(
            _create(code='TRAFONLY', balance_bonus_kopeks=0, subscription_days=0, traffic_gb=50), db=db
        )

        stored = await db.get(PromoCode, created.id)
        assert (stored.balance_bonus_kopeks, stored.subscription_days, stored.traffic_gb) == (0, 0, 50)


@pytest.mark.asyncio
async def test_update_cannot_empty_a_live_bonus_set(monkeypatch):
    """Правка не должна обнулять живой набор до кода, который ничего не даёт."""
    async with memory_session(monkeypatch, TABLES) as db:
        created = await create_promocode_endpoint(_create(traffic_gb=0), db=db)

        with pytest.raises(HTTPException) as exc:
            await update_promocode_endpoint(
                created.id, PromoCodeUpdateRequest(balance_bonus_kopeks=0, subscription_days=0), db=db
            )
        assert exc.value.status_code == 400

        stored = await db.get(PromoCode, created.id)
        assert stored.balance_bonus_kopeks == 10000
        assert stored.subscription_days == 7


@pytest.mark.asyncio
async def test_update_may_empty_days_when_traffic_remains(monkeypatch):
    """Обнулить дни можно, если в наборе остаётся трафик — набор непустой."""
    async with memory_session(monkeypatch, TABLES) as db:
        created = await create_promocode_endpoint(_create(traffic_gb=50), db=db)

        updated = await update_promocode_endpoint(
            created.id, PromoCodeUpdateRequest(balance_bonus_kopeks=0, subscription_days=0), db=db
        )

        assert updated.traffic_gb == 50
        assert updated.subscription_days == 0


@pytest.mark.asyncio
async def test_negative_traffic_rejected(monkeypatch):
    """Отрицательный трафик отклоняется и на создании, и на правке."""
    async with memory_session(monkeypatch, TABLES) as db:
        with pytest.raises(HTTPException) as exc:
            await create_promocode_endpoint(_create(code='NEG', traffic_gb=-5), db=db)
        assert exc.value.status_code == 400

        created = await create_promocode_endpoint(_create(traffic_gb=10), db=db)
        with pytest.raises(HTTPException) as exc:
            await update_promocode_endpoint(created.id, PromoCodeUpdateRequest(traffic_gb=-1), db=db)
        assert exc.value.status_code == 400
