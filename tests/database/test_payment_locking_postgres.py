"""Гарантии платежей, которые проверяются только на настоящем PostgreSQL.

Всё остальное про TabPay и ParityPay покрыто на in-memory SQLite. Но SQLite
молча игнорирует ``FOR UPDATE``, не проверяет длину ``VARCHAR(n)`` и не хранит
часовой пояс — то есть главная защита от двойного зачисления там не проверяется
вообще, а выглядит проверенной. Здесь эти места закрываются на том же движке,
на котором работает прод.

Каждая проверка ниже обязана падать, если из CRUD убрать ``.with_for_update()``
или ``populate_existing=True``. Обе мутации прогонялись — см. docs/handoffs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.database.crud.paritypay as paritypay_crud
import app.database.crud.tabpay as tabpay_crud
from app.database.models import ParityPayPayment, TabPayPayment, User
from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter


pytestmark = pytest.mark.postgres


GATEWAYS = [
    pytest.param(tabpay_crud, TabPayPayment, 'tabpay', id='tabpay'),
    pytest.param(paritypay_crud, ParityPayPayment, 'paritypay', id='paritypay'),
]

# Обе таблицы платежей плюс users: платёж ссылается на пользователя внешним
# ключом, и PostgreSQL, в отличие от SQLite, этот ключ действительно проверяет.
PAYMENT_TABLES = [TabPayPayment.__table__, ParityPayPayment.__table__, User.__table__]

LOCK_WAIT_TIMEOUT_MS = 400


def _fn(module: Any, name: str):
    return getattr(module, name)


async def _create_user(db: AsyncSession, telegram_id: int = 1000001) -> User:
    """Настоящая строка пользователя: внешний ключ платежа её требует."""
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _create_payment(module: Any, prefix: str, db: AsyncSession, *, user_id: Any, **overrides: Any):
    kwargs = {
        'user_id': user_id,
        'order_id': f'{prefix}1_abcdef12',
        'amount_kopeks': 125000,
        'description': 'Пополнение баланса',
        'payment_url': 'https://pay.example/x',
        'payment_method': 'sbp',
        f'{prefix}_payment_id': 'ext-1',
        **overrides,
    }
    return await _fn(module, f'create_{prefix}_payment')(db=db, **kwargs)


async def _set_lock_timeout(db: AsyncSession, milliseconds: int) -> None:
    """Ограничивает ожидание блокировки, чтобы тест не висел бесконечно.

    ``SET LOCAL`` действует до конца транзакции и заодно её открывает.
    """
    await db.execute(text(f"SET LOCAL lock_timeout = '{milliseconds}ms'"))


# --------------------------------------------------------------------------
# 1. Блокировка строки
# --------------------------------------------------------------------------


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_for_update_blocks_second_session(postgres_database, module, model, prefix) -> None:
    """Вторая сессия обязана ЖДАТЬ строку, занятую первой.

    Это и есть защита от двойного зачисления при параллельной доставке
    вебхука. На SQLite тот же код возвращает строку мгновенно.
    """
    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=2) as (first, second):
        user = await _create_user(first)
        payment = await _create_payment(module, prefix, first, user_id=user.id)

        locked = await _fn(module, f'get_{prefix}_payment_by_id_for_update')(first, payment.id)
        assert locked is not None, 'первая сессия не смогла взять строку'

        await _set_lock_timeout(second, LOCK_WAIT_TIMEOUT_MS)
        with pytest.raises(DBAPIError) as failure:
            await _fn(module, f'get_{prefix}_payment_by_id_for_update')(second, payment.id)

        assert 'lock timeout' in str(failure.value).lower(), (
            'вторая сессия не ждала блокировку — FOR UPDATE не работает'
        )


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_plain_read_does_not_block(postgres_database, module, model, prefix) -> None:
    """Контроль к предыдущему тесту: ожидание вызвано именно блокировкой.

    Без него «вторая сессия ждала» могло бы означать что угодно — например,
    что строка недоступна сама по себе. Обычное чтение той же строки под тем
    же lock_timeout проходит мгновенно.
    """
    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=2) as (first, second):
        user = await _create_user(first)
        payment = await _create_payment(module, prefix, first, user_id=user.id)
        await _fn(module, f'get_{prefix}_payment_by_id_for_update')(first, payment.id)

        await _set_lock_timeout(second, LOCK_WAIT_TIMEOUT_MS)
        visible = await _fn(module, f'get_{prefix}_payment_by_id')(second, payment.id)

        assert visible is not None
        assert visible.order_id == payment.order_id


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_waiter_sees_committed_changes_after_lock_release(postgres_database, module, model, prefix) -> None:
    """Дождавшись блокировки, вторая сессия читает уже НОВОЕ значение.

    Именно на этом держится повторная проверка ``processed_events`` под
    блокировкой: без свежего чтения она смотрела бы в устаревший объект.
    """
    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=3) as (first, second, watcher):
        user = await _create_user(first)
        payment = await _create_payment(module, prefix, first, user_id=user.id)

        # Вторая сессия читает строку ДО изменений — так же, как обработчик
        # вебхука читает платёж по order_id перед захватом блокировки.
        stale = await _fn(module, f'get_{prefix}_payment_by_order_id')(second, payment.order_id)
        assert stale is not None
        assert stale.status == 'pending'

        locked = await _fn(module, f'get_{prefix}_payment_by_id_for_update')(first, payment.id)

        async def hold_then_commit() -> None:
            await wait_for_lock_waiter(watcher)
            _fn(module, f'remember_{prefix}_event')(locked, 'ext-1:PAID')
            locked.status = 'success'
            locked.is_paid = True
            await first.commit()

        async def wait_for_row():
            return await _fn(module, f'get_{prefix}_payment_by_id_for_update')(second, payment.id)

        _, fresh = await asyncio.gather(hold_then_commit(), wait_for_row())

        assert fresh is not None
        assert fresh.status == 'success', 'дождавшаяся сессия увидела устаревшую строку'
        assert fresh.processed_events == ['ext-1:PAID']
        assert _fn(module, f'is_{prefix}_event_processed')(fresh, 'ext-1:PAID') is True


# --------------------------------------------------------------------------
# 2. Гонка двух одновременных уведомлений
# --------------------------------------------------------------------------


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_duplicate_delivery_credits_exactly_once(postgres_database, module, model, prefix) -> None:
    """Два одновременных вебхука об одном событии — ровно одно зачисление.

    Повторяется критическая секция ``process_*_callback``: чтение по order_id,
    проверка ``processed_events``, захват строки, повторная проверка под
    блокировкой. Обе доставки успевают пройти ПЕРВУЮ проверку — отсеять
    вторую может только блокировка.
    """
    earlier_key = 'ext-1:PENDING'
    event_key = 'ext-1:PAID'

    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=3) as (first, second, watcher):
        user = await _create_user(first)
        payment = await _create_payment(module, prefix, first, user_id=user.id)
        payment_id = payment.id

        # Одно событие по этому платежу уже обработано: провайдер присылает
        # несколько статусов подряд. Пустой список скрыл бы целый класс ошибок —
        # добавление события «на месте» вместо пересборки списка не помечает
        # JSON-колонку изменённой, и UPDATE до базы не доходит.
        _fn(module, f'remember_{prefix}_event')(payment, earlier_key)
        await first.commit()

        # Обе доставки читают платёж до того, как хоть одна взяла блокировку.
        for session in (first, second):
            seen = await _fn(module, f'get_{prefix}_payment_by_order_id')(session, payment.order_id)
            assert not _fn(module, f'is_{prefix}_event_processed')(seen, event_key)

        holder_took_the_row = asyncio.Event()

        async def finish(session: AsyncSession, locked: Any) -> str:
            if _fn(module, f'is_{prefix}_event_processed')(locked, event_key):
                await session.rollback()
                return 'duplicate'

            _fn(module, f'remember_{prefix}_event')(locked, event_key)
            locked.status = 'success'
            locked.is_paid = True
            await session.commit()
            return 'credited'

        async def holder(session: AsyncSession) -> str:
            locked = await _fn(module, f'get_{prefix}_payment_by_id_for_update')(session, payment_id)
            holder_took_the_row.set()
            # Держим строку, пока соперник не встанет в очередь. Отпустить раньше
            # — значит проверить не блокировку, а удачное совпадение по времени:
            # соперник прочитал бы уже закоммиченные данные обычным SELECT.
            await wait_for_lock_waiter(watcher)
            return await finish(session, locked)

        async def follower(session: AsyncSession) -> str:
            await holder_took_the_row.wait()
            locked = await _fn(module, f'get_{prefix}_payment_by_id_for_update')(session, payment_id)
            return await finish(session, locked)

        outcomes = await asyncio.gather(holder(first), follower(second))

        assert sorted(outcomes) == ['credited', 'duplicate'], f'зачислений должно быть ровно одно, получено: {outcomes}'

        reread = await _fn(module, f'get_{prefix}_payment_by_id')(watcher, payment_id)
        assert reread.processed_events == [earlier_key, event_key], 'событие не долетело до базы'
        assert reread.is_paid is True


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_duplicate_order_id_loses_race_on_unique_index(postgres_database, module, model, prefix) -> None:
    """Уникальность order_id обеспечивает БД, а не проверка перед вставкой.

    Сетевой сбой при создании платежа приводит к повтору — две вставки могут
    уйти одновременно. Выжить должна одна.
    """
    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=3) as (first, second, watcher):
        user = await _create_user(first)
        user_id = user.id

        async def insert(session: AsyncSession, external_id: str):
            try:
                await _create_payment(
                    module,
                    prefix,
                    session,
                    user_id=user_id,
                    order_id=f'{prefix}_race_1',
                    **{f'{prefix}_payment_id': external_id},
                )
            except IntegrityError:
                await session.rollback()
                return 'rejected'
            return 'created'

        outcomes = await asyncio.gather(insert(first, 'ext-a'), insert(second, 'ext-b'))

        assert sorted(outcomes) == ['created', 'rejected']

        rows = await watcher.execute(
            text(f'SELECT count(*) FROM {model.__tablename__} WHERE order_id = :oid'), {'oid': f'{prefix}_race_1'}
        )
        assert rows.scalar() == 1


# --------------------------------------------------------------------------
# 3. Ограничения схемы
# --------------------------------------------------------------------------


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_order_id_longer_than_column_is_rejected(postgres_database, module, model, prefix) -> None:
    """VARCHAR(64) на order_id — не декорация: обрезка в клиенте обязательна.

    SQLite примет строку любой длины и промолчит, PostgreSQL откажет. Если
    убрать ``order_id[:64]`` из клиента, платёж не создастся вовсе.
    """
    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=1) as (db,):
        user = await _create_user(db)

        with pytest.raises(DBAPIError) as failure:
            await _create_payment(module, prefix, db, user_id=user.id, order_id='x' * 65)

        assert 'too long' in str(failure.value).lower()
        await db.rollback()


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_payment_without_user_is_rejected(postgres_database, module, model, prefix) -> None:
    """Внешний ключ на users PostgreSQL проверяет, SQLite — нет."""
    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=1) as (db,):
        with pytest.raises(IntegrityError):
            await _create_payment(module, prefix, db, user_id=999999)
        await db.rollback()


# --------------------------------------------------------------------------
# 4. Типы данных
# --------------------------------------------------------------------------


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_timestamps_keep_the_same_instant_across_timezones(postgres_database, module, model, prefix) -> None:
    """TIMESTAMPTZ хранит момент времени, а не текст с числами.

    Провайдеры присылают срок жизни счёта в своей зоне. Записанное как +05:00
    обязано вернуться тем же моментом — иначе платёж «истечёт» на пять часов
    раньше или позже.
    """
    expires_local = datetime(2026, 8, 31, 17, 0, tzinfo=timezone(timedelta(hours=5)))

    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=1) as (db,):
        user = await _create_user(db)
        created = await _create_payment(module, prefix, db, user_id=user.id, expires_at=expires_local)
        db.expunge_all()

        fetched = await _fn(module, f'get_{prefix}_payment_by_id')(db, created.id)

        assert fetched.expires_at is not None
        assert fetched.expires_at.tzinfo is not None, 'время вернулось наивным — часовой пояс потерян'
        assert fetched.expires_at == expires_local
        assert fetched.expires_at.utcoffset() == timedelta(0), 'PostgreSQL обязан отдавать TIMESTAMPTZ в UTC'
        assert fetched.expires_at == datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_json_columns_survive_round_trip(postgres_database, module, model, prefix) -> None:
    """JSON-колонки возвращаются ровно тем же деревом значений.

    В ``metadata_json`` лежат данные покупки с лендинга, в ``callback_payload``
    — тело уведомления. Потеря вложенности или типа числа тут означает
    неправильно выданный товар.
    """
    metadata = {
        'purpose': 'balance_topup',
        'guest_token': 'токен-пробел и юникод',
        'nested': {'periods': [30, 90, 180], 'ratio': 0.5, 'flag': True, 'none': None},
    }
    callback = {'status': 'PAID', 'amountKopecks': 125000, 'metadata': metadata}

    async with postgres_sessions(postgres_database, PAYMENT_TABLES, count=1) as (db,):
        user = await _create_user(db)
        created = await _create_payment(module, prefix, db, user_id=user.id, metadata_json=metadata)

        await _fn(module, f'update_{prefix}_payment_status')(
            db=db,
            payment=created,
            status='success',
            is_paid=True,
            callback_payload=callback,
        )
        db.expunge_all()

        fetched = await _fn(module, f'get_{prefix}_payment_by_id')(db, created.id)

        assert fetched.metadata_json == metadata
        assert fetched.callback_payload == callback
        assert fetched.callback_payload['amountKopecks'] == 125000
        assert isinstance(fetched.callback_payload['amountKopecks'], int)
