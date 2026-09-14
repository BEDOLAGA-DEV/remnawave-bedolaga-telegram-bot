"""CRUD платежей TabPay и ParityPay на настоящей БД.

До этого CRUD проверялся только через фейковые объекты, и целый класс ошибок
оставался невидимым: изменение прошло бы все проверки в памяти, но не долетело
до UPDATE. Особенно это касается ``processed_events`` — на нём держится
идемпотентность: не сохранился список обработанных событий, и после
перезапуска повторное уведомление зачислит баланс второй раз.

Здесь всё пишется в in-memory SQLite и перечитывается ОТДЕЛЬНЫМ запросом с
предварительным expunge, чтобы значение пришло из базы, а не из кеша сессии.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

import app.database.crud.paritypay as paritypay_crud
import app.database.crud.tabpay as tabpay_crud
from app.database.models import ParityPayPayment, TabPayPayment
from tests.fixtures.sqlite_memory import memory_session


GATEWAYS = [
    pytest.param(tabpay_crud, TabPayPayment, 'tabpay', id='tabpay'),
    pytest.param(paritypay_crud, ParityPayPayment, 'paritypay', id='paritypay'),
]


def _fn(module: Any, name: str):
    return getattr(module, name)


async def _create(module: Any, prefix: str, db: Any, **overrides: Any):
    kwargs = {
        'user_id': 1,
        'order_id': f'{prefix}1_abcdef12',
        'amount_kopeks': 125000,
        'description': 'Пополнение баланса',
        'payment_url': 'https://pay.example/x',
        'payment_method': 'sbp',
        f'{prefix}_payment_id': 'ext-1',
        **overrides,
    }
    return await _fn(module, f'create_{prefix}_payment')(db=db, **kwargs)


async def _reread(db: Any, model: Any, payment_id: int, module: Any, prefix: str):
    """Перечитать строку из БД, а не из кеша сессии."""
    db.expunge_all()
    return await _fn(module, f'get_{prefix}_payment_by_id')(db, payment_id)


# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_create_round_trip(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)

        assert created.id is not None
        assert created.status == 'pending'
        assert created.is_paid is False
        assert created.processed_events == []

        fetched = await _reread(db, model, created.id, module, prefix)
        assert fetched is not None
        assert fetched.order_id == created.order_id
        assert fetched.amount_kopeks == 125000
        assert fetched.payment_url == 'https://pay.example/x'
        assert getattr(fetched, f'{prefix}_payment_id') == 'ext-1'


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_lookup_by_order_id_and_invoice_id(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)
        db.expunge_all()

        by_order = await _fn(module, f'get_{prefix}_payment_by_order_id')(db, created.order_id)
        by_invoice = await _fn(module, f'get_{prefix}_payment_by_invoice_id')(db, 'ext-1')

        assert by_order is not None and by_order.id == created.id
        assert by_invoice is not None and by_invoice.id == created.id
        assert await _fn(module, f'get_{prefix}_payment_by_order_id')(db, 'нет такого') is None


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_order_id_is_unique_in_database(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    """Два платежа с одним order_id разошлись бы по одному уведомлению."""
    async with memory_session(monkeypatch, [model.__table__]) as db:
        await _create(module, prefix, db)

        with pytest.raises(IntegrityError):
            await _create(module, prefix, db)


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_processed_events_survive_commit(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    """Главная проверка: список обработанных событий обязан долетать до БД.

    Мутация списка на месте не помечает JSON-колонку изменённой, UPDATE не
    уходит, и после перезапуска идемпотентность теряется молча.
    """
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)

        _fn(module, f'remember_{prefix}_event')(created, 'ext-1:PAID')
        await db.commit()

        fetched = await _reread(db, model, created.id, module, prefix)
        assert fetched.processed_events == ['ext-1:PAID'], 'событие не сохранилось в БД'
        assert _fn(module, f'is_{prefix}_event_processed')(fetched, 'ext-1:PAID') is True
        assert _fn(module, f'is_{prefix}_event_processed')(fetched, 'ext-1:REFUNDED') is False


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_second_event_appends_in_database(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    """Возврат после оплаты — второе событие, первое не должно пропасть."""
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)

        _fn(module, f'remember_{prefix}_event')(created, 'ext-1:PAID')
        await db.commit()
        reloaded = await _reread(db, model, created.id, module, prefix)

        _fn(module, f'remember_{prefix}_event')(reloaded, 'ext-1:REFUNDED')
        await db.commit()

        final = await _reread(db, model, created.id, module, prefix)
        assert final.processed_events == ['ext-1:PAID', 'ext-1:REFUNDED']


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_update_status_persists(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)

        await _fn(module, f'update_{prefix}_payment_status')(
            db=db,
            payment=created,
            status='success',
            is_paid=True,
            callback_payload={'status': 'PAID'},
        )

        fetched = await _reread(db, model, created.id, module, prefix)
        assert fetched.status == 'success'
        assert fetched.is_paid is True
        assert fetched.paid_at is not None
        assert fetched.callback_payload == {'status': 'PAID'}


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_update_status_keeps_is_paid_when_not_given(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    """is_paid=None означает «не трогать»: возврат не должен обнулять факт оплаты."""
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)
        await _fn(module, f'update_{prefix}_payment_status')(db=db, payment=created, status='success', is_paid=True)

        await _fn(module, f'update_{prefix}_payment_status')(db=db, payment=created, status='refunded', is_paid=None)

        fetched = await _reread(db, model, created.id, module, prefix)
        assert fetched.status == 'refunded'
        assert fetched.is_paid is True


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_link_to_transaction_persists(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)

        await _fn(module, f'link_{prefix}_payment_to_transaction')(db, payment=created, transaction_id=4242)
        await db.commit()

        fetched = await _reread(db, model, created.id, module, prefix)
        assert fetched.transaction_id == 4242


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_pending_list_excludes_paid(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    async with memory_session(monkeypatch, [model.__table__]) as db:
        pending = await _create(module, prefix, db, order_id=f'{prefix}1_pending')
        paid = await _create(module, prefix, db, order_id=f'{prefix}1_paid', **{f'{prefix}_payment_id': 'ext-2'})
        await _fn(module, f'update_{prefix}_payment_status')(db=db, payment=paid, status='success', is_paid=True)
        db.expunge_all()

        rows = await _fn(module, f'get_pending_{prefix}_payments')(db, 1)

        assert [r.order_id for r in rows] == [pending.order_id]


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_for_update_lock_is_not_verifiable_on_sqlite(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    """SQLite молча игнорирует FOR UPDATE — блокировка тут НЕ проверяется.

    Тест фиксирует границу честности: запрос отрабатывает и возвращает строку,
    но никакой блокировки за этим нет. Настоящая проверка конкурентного доступа
    возможна только на PostgreSQL.
    """
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db)
        db.expunge_all()

        locked = await _fn(module, f'get_{prefix}_payment_by_id_for_update')(db, created.id)

        assert locked is not None
        assert locked.id == created.id


@pytest.mark.parametrize(('module', 'model', 'prefix'), GATEWAYS)
async def test_expires_at_round_trip(monkeypatch, module: Any, model: Any, prefix: str) -> None:
    """Дата истечения должна возвращаться с часовым поясом, а не наивной."""
    expires = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    async with memory_session(monkeypatch, [model.__table__]) as db:
        created = await _create(module, prefix, db, expires_at=expires)
        db.expunge_all()

        fetched = await _reread(db, model, created.id, module, prefix)
        assert fetched.expires_at is not None
        assert fetched.expires_at.tzinfo is not None
        assert fetched.expires_at == expires
