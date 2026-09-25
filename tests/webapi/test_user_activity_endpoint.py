"""Таймлайн активности доступен внешнему Web API, а не только админке кабинета.

Ленту собирает `app.services.user_activity_service`; кабинетная ручка и ручка
Web API — два тонких слоя над ним. Сторож следит, что внешняя ручка отдаёт те же
записи, принимает telegram_id наравне с внутренним id и честно ругается на
неизвестный тип, вместо того чтобы молча вернуть пустую ленту.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.database.models import Base, Transaction, TransactionType, User, UserStatus
from app.webapi.routes.users import get_user_activity
from tests.fixtures.sqlite_memory import memory_session


# Пользователь тянет связи (промогруппы, подписки) — поднимаем всю схему.
TABLES = tuple(Base.metadata.sorted_tables)


async def _seed(db) -> User:
    user = User(
        telegram_id=777000111,
        username='activity_probe',
        first_name='Пробный',
        status=UserStatus.ACTIVE.value,
        balance_kopeks=0,
        language='ru',
    )
    db.add(user)
    await db.flush()

    now = datetime.now(UTC)
    db.add_all(
        [
            Transaction(
                user_id=user.id,
                type=TransactionType.DEPOSIT.value,
                amount_kopeks=45000,
                description='Пополнение баланса',
                payment_method='yookassa',
                is_completed=True,
                created_at=now - timedelta(minutes=5),
            ),
            Transaction(
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                amount_kopeks=45000,
                description='Продление подписки',
                is_completed=True,
                created_at=now - timedelta(minutes=4),
            ),
        ]
    )
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_activity_returns_timeline_for_internal_id(monkeypatch):
    async with memory_session(monkeypatch, list(TABLES)) as db:
        user = await _seed(db)

        response = await get_user_activity(user_id=user.id, _=None, db=db, offset=0, limit=50, types=None)

        assert response.total == 2
        assert [item.type for item in response.items] == ['transaction', 'transaction']
        # свежая запись сверху
        assert response.items[0].title == 'Продление подписки'


@pytest.mark.asyncio
async def test_activity_accepts_telegram_id(monkeypatch):
    async with memory_session(monkeypatch, list(TABLES)) as db:
        user = await _seed(db)

        response = await get_user_activity(user_id=user.telegram_id, _=None, db=db, offset=0, limit=50, types=None)

        assert response.total == 2


@pytest.mark.asyncio
async def test_activity_filters_by_type(monkeypatch):
    async with memory_session(monkeypatch, list(TABLES)) as db:
        user = await _seed(db)

        response = await get_user_activity(user_id=user.id, _=None, db=db, offset=0, limit=50, types='transaction')

        assert response.total == 2
        assert all(item.type == 'transaction' for item in response.items)


@pytest.mark.asyncio
async def test_activity_rejects_unknown_type(monkeypatch):
    async with memory_session(monkeypatch, list(TABLES)) as db:
        user = await _seed(db)

        with pytest.raises(HTTPException) as exc:
            await get_user_activity(user_id=user.id, _=None, db=db, offset=0, limit=50, types='rocket_launch')

        assert exc.value.status_code == 400
        assert 'rocket_launch' in str(exc.value.detail)


@pytest.mark.asyncio
async def test_activity_404_for_missing_user(monkeypatch):
    async with memory_session(monkeypatch, list(TABLES)) as db:
        with pytest.raises(HTTPException) as exc:
            await get_user_activity(user_id=99999, _=None, db=db, offset=0, limit=50, types=None)

        assert exc.value.status_code == 404
