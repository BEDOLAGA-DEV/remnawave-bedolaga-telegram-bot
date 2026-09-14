"""Блокировка строки пользователя при изменении баланса — на настоящем PostgreSQL.

``lock_user_for_update`` — единственная защита баланса от потерянного
обновления, и она общая для ВСЕХ платёжных шлюзов, а не только для новых. На
SQLite ``FOR UPDATE`` игнорируется, поэтому там этот код проверить нельзя в
принципе: тест зеленел бы и с полностью снятой блокировкой.

Классический сценарий потерянного обновления: два зачисления читают баланс 0,
каждое прибавляет свою сумму и пишет результат — второе затирает первое, и
деньги пропадают.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import lock_user_for_update
from app.database.models import User
from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter


pytestmark = pytest.mark.postgres


USER_TABLES = [User.__table__]

LOCK_WAIT_TIMEOUT_MS = 400
TOPUP_KOPEKS = 125000


async def _create_user(db: AsyncSession, *, telegram_id: int = 2000001, balance_kopeks: int = 0) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', balance_kopeks=balance_kopeks)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _load_user(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one()


async def test_user_lock_blocks_second_session(postgres_database) -> None:
    """Пока одно зачисление держит строку пользователя, второе ждёт."""
    async with postgres_sessions(postgres_database, USER_TABLES, count=2) as (first, second):
        user = await _create_user(first)
        user_id = user.id

        await lock_user_for_update(first, user)

        await second.execute(text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_TIMEOUT_MS}ms'"))
        waiting_user = await _load_user(second, user_id)
        with pytest.raises(DBAPIError) as failure:
            await lock_user_for_update(second, waiting_user)

        assert 'lock timeout' in str(failure.value).lower(), 'строка пользователя не блокируется'


async def test_concurrent_topups_do_not_lose_money(postgres_database) -> None:
    """Два одновременных зачисления складываются, а не затирают друг друга.

    Без ``FOR UPDATE`` оба читают баланс 0, оба пишут 125000, и одно
    пополнение исчезает — покупатель заплатил дважды, а получил один раз.
    """
    async with postgres_sessions(postgres_database, USER_TABLES, count=3) as (first, second, watcher):
        user = await _create_user(first)
        user_id = user.id

        holder_took_the_row = asyncio.Event()

        async def credit(session: AsyncSession, *, hold: bool) -> int:
            # Каждое зачисление сначала читает пользователя обычным запросом —
            # ровно как это делает _finalize_*_payment перед блокировкой.
            own_view = await _load_user(session, user_id)
            assert own_view.balance_kopeks == 0, 'оба зачисления обязаны стартовать с одного значения'

            if not hold:
                await holder_took_the_row.wait()

            locked = await lock_user_for_update(session, own_view)
            if hold:
                holder_took_the_row.set()
                # Держим строку, пока соперник не встанет в очередь: иначе тест
                # проверял бы удачное совпадение по времени, а не блокировку.
                await wait_for_lock_waiter(watcher)

            locked.balance_kopeks += TOPUP_KOPEKS
            await session.commit()
            return locked.balance_kopeks

        balances = await asyncio.gather(credit(first, hold=True), credit(second, hold=False))

        assert sorted(balances) == [TOPUP_KOPEKS, 2 * TOPUP_KOPEKS], (
            f'второе зачисление читало устаревший баланс: {balances}'
        )

        final = await _load_user(watcher, user_id)
        assert final.balance_kopeks == 2 * TOPUP_KOPEKS, 'пополнение потерялось'


async def test_lock_returns_fresh_values_not_the_cached_object(postgres_database) -> None:
    """Блокировка обязана отдавать значения из БД, а не из кеша сессии.

    ``populate_existing=True`` в ``lock_user_for_update`` держится именно на
    этом: без него дождавшаяся сессия увидела бы свой прежний снимок баланса.
    """
    async with postgres_sessions(postgres_database, USER_TABLES, count=2) as (first, second):
        user = await _create_user(first)
        user_id = user.id

        # Вторая сессия сделала свой снимок до изменения.
        stale = await _load_user(second, user_id)
        assert stale.balance_kopeks == 0

        locked = await lock_user_for_update(first, user)
        locked.balance_kopeks = TOPUP_KOPEKS
        await first.commit()

        fresh = await lock_user_for_update(second, stale)

        assert fresh.balance_kopeks == TOPUP_KOPEKS, 'блокировка вернула устаревший объект из кеша сессии'
