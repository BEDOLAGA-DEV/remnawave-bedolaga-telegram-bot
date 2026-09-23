"""Успешные СБП-автопродления Platega видны в админке платежей (issue #3279).

Коллбек рекуррентного списания не создаёт строку в ``platega_payments``: он
пишет только ``Transaction`` типа SUBSCRIPTION_PAYMENT с методом ``platega``.
Админка же читала исключительно таблицы провайдеров, поэтому успешные
автопродления в ней не появлялись вовсе.

Здесь проверяется, что они появились — и что при этом не поехало остальное:
фиктивных pending-строк не завелось, обычные пополнения не задвоились, а
внутренние списания с баланса за внешние платежи не выдаются.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import (
    Base,
    PaymentMethod,
    PlategaPayment,
    Transaction,
    TransactionType,
    User,
)
from app.services.payment_verification_service import (
    SUPPORTED_MANUAL_CHECK_METHODS,
    get_payment_record,
    list_recent_pending_payments,
)
from tests.fixtures.sqlite_memory import memory_session


# Загрузчик опрашивает таблицы всех провайдеров сразу, поэтому поднимаем
# полную схему: список «только нужных» таблиц пришлось бы править при каждом
# новом шлюзе, а тест падал бы с невнятным «no such table».
TABLES = list(Base.metadata.tables.values())

CHARGE_ID = 'charge-abc-1'


def _user(user_id: int = 1) -> User:
    return User(
        id=user_id,
        telegram_id=1000 + user_id,
        first_name=f'U{user_id}',
        language='ru',
        status='active',
        balance_kopeks=0,
    )


def _recurring_charge(*, user_id: int = 1, external_id: str = CHARGE_ID, **over) -> Transaction:
    """Транзакция, какую пишет коллбек СБП-автопродления Platega."""
    fields = {
        'user_id': user_id,
        'type': TransactionType.SUBSCRIPTION_PAYMENT.value,
        'amount_kopeks': 29900,
        'description': 'СБП-автопродление Platega',
        'payment_method': PaymentMethod.PLATEGA.value,
        'external_id': external_id,
        'is_completed': True,
        'created_at': datetime.now(UTC) - timedelta(hours=1),
    }
    fields.update(over)
    return Transaction(**fields)


@pytest.mark.asyncio
async def test_successful_recurring_charge_is_listed(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(), _recurring_charge()])
        await db.commit()

        records = await list_recent_pending_payments(db)

    recurring = [r for r in records if r.method == PaymentMethod.PLATEGA_RECURRENT]
    assert len(recurring) == 1

    record = recurring[0]
    assert record.is_paid is True
    assert record.amount_kopeks == 29900
    # Идентификатор списания — то, по чему платёж ищут в Platega.
    assert record.identifier == CHARGE_ID
    assert record.user.telegram_id == 1001


@pytest.mark.asyncio
async def test_balance_debit_is_not_shown_as_external_payment(monkeypatch) -> None:
    # Списание с баланса — внутренняя операция, платёжного шлюза за ней нет.
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(),
                _recurring_charge(
                    payment_method=PaymentMethod.BALANCE.value,
                    external_id=None,
                    description='Продление с баланса',
                ),
            ]
        )
        await db.commit()

        records = await list_recent_pending_payments(db)

    assert [r for r in records if r.method == PaymentMethod.PLATEGA_RECURRENT] == []


@pytest.mark.asyncio
async def test_ordinary_platega_topup_is_not_duplicated(monkeypatch) -> None:
    """Пополнение через Platega уже видно по своей строке в platega_payments.

    У него тип DEPOSIT, и попасть в выборку автопродлений оно не должно —
    иначе один платёж показывался бы в списке дважды.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(),
                PlategaPayment(
                    id=1,
                    user_id=1,
                    amount_kopeks=50000,
                    status='PENDING',
                    is_paid=False,
                    created_at=datetime.now(UTC) - timedelta(minutes=5),
                    platega_transaction_id='tx-1',
                    correlation_id='corr-1',
                    payment_method_code=2,
                ),
                _recurring_charge(
                    type=TransactionType.DEPOSIT.value,
                    external_id='tx-1',
                    description='Пополнение через Platega',
                ),
            ]
        )
        await db.commit()

        records = await list_recent_pending_payments(db)

    assert [r.method for r in records].count(PaymentMethod.PLATEGA) == 1
    assert [r for r in records if r.method == PaymentMethod.PLATEGA_RECURRENT] == []


@pytest.mark.asyncio
async def test_pending_platega_payment_still_listed(monkeypatch) -> None:
    """Существующая логика pending-платежей не изменилась."""
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(),
                PlategaPayment(
                    id=7,
                    user_id=1,
                    amount_kopeks=50000,
                    status='PENDING',
                    is_paid=False,
                    created_at=datetime.now(UTC) - timedelta(minutes=5),
                    platega_transaction_id='tx-pending',
                    correlation_id='corr-pending',
                    payment_method_code=2,
                ),
            ]
        )
        await db.commit()

        records = await list_recent_pending_payments(db)

    pending = [r for r in records if r.method == PaymentMethod.PLATEGA]
    assert len(pending) == 1
    assert pending[0].identifier == 'tx-pending'
    assert pending[0].is_paid is False


@pytest.mark.asyncio
async def test_details_do_not_confuse_ids_between_tables(monkeypatch) -> None:
    """Одинаковый номер в разных таблицах не должен открывать чужой платёж.

    Список отдаёт автопродление с id транзакции. Детали открываются роутом
    ``/{method}/{payment_id}``, и если бы автопродление отдавалось под методом
    ``platega``, админ по этому номеру получил бы строку platega_payments —
    другой платёж, возможно другого человека.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(1),
                _user(2),
                # Обе записи получают id = 1, но в разных таблицах.
                _recurring_charge(id=1, user_id=1),
                PlategaPayment(
                    id=1,
                    user_id=2,
                    amount_kopeks=50000,
                    status='PENDING',
                    is_paid=False,
                    created_at=datetime.now(UTC) - timedelta(minutes=5),
                    platega_transaction_id='tx-other',
                    correlation_id='corr-other',
                    payment_method_code=2,
                ),
            ]
        )
        await db.commit()

        recurring = await get_payment_record(db, PaymentMethod.PLATEGA_RECURRENT, 1)
        provider = await get_payment_record(db, PaymentMethod.PLATEGA, 1)

        assert recurring is not None
        assert recurring.identifier == CHARGE_ID
        assert recurring.user.id == 1

        assert provider is not None
        assert provider.identifier == 'tx-other'
        assert provider.user.id == 2


@pytest.mark.asyncio
async def test_details_reject_foreign_transaction(monkeypatch) -> None:
    """По номеру чужой транзакции автопродление не отдаётся.

    Та же защита, что у звёзд: загрузив строку по id, сверяем тип и метод.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(),
                _recurring_charge(
                    id=5,
                    type=TransactionType.DEPOSIT.value,
                    description='Пополнение через Platega',
                ),
            ]
        )
        await db.commit()

        assert await get_payment_record(db, PaymentMethod.PLATEGA_RECURRENT, 5) is None


def test_recurring_charge_has_no_manual_check() -> None:
    """Проверять статус нечего: у списания нет счёта в platega_payments."""
    assert PaymentMethod.PLATEGA_RECURRENT not in SUPPORTED_MANUAL_CHECK_METHODS


@pytest.mark.asyncio
async def test_search_finds_recurring_charge_by_charge_id(monkeypatch) -> None:
    """Поиск по номеру списания находит автопродление.

    Список и поиск — разные источники данных: список собирает загрузчики,
    поиск идёт по карте провайдеров. Зарегистрировать автопродление нужно в
    обоих, иначе платёж виден в списке, но не находится.
    """
    from app.services.payment_search_service import (
        PeriodPreset,
        SearchParams,
        StatusFilter,
        search_payments,
    )

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(), _recurring_charge()])
        await db.commit()

        found, total = await search_payments(
            db,
            SearchParams(
                search=CHARGE_ID,
                status_filter=StatusFilter.ALL,
                method_filter=None,
                period=PeriodPreset.ALL,
                page=1,
                per_page=20,
            ),
        )

    assert total == 1
    assert found[0].method == PaymentMethod.PLATEGA_RECURRENT
    assert found[0].identifier == CHARGE_ID


@pytest.mark.asyncio
async def test_search_filter_by_method_returns_only_recurring(monkeypatch) -> None:
    from app.services.payment_search_service import (
        PeriodPreset,
        SearchParams,
        StatusFilter,
        search_payments,
    )

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all(
            [
                _user(),
                _recurring_charge(),
                PlategaPayment(
                    id=1,
                    user_id=1,
                    amount_kopeks=50000,
                    status='PENDING',
                    is_paid=False,
                    created_at=datetime.now(UTC) - timedelta(minutes=5),
                    platega_transaction_id='tx-1',
                    correlation_id='corr-1',
                    payment_method_code=2,
                ),
            ]
        )
        await db.commit()

        found, total = await search_payments(
            db,
            SearchParams(
                search=None,
                status_filter=StatusFilter.ALL,
                method_filter=PaymentMethod.PLATEGA_RECURRENT,
                period=PeriodPreset.ALL,
                page=1,
                per_page=20,
            ),
        )

    assert total == 1
    assert found[0].method == PaymentMethod.PLATEGA_RECURRENT


def test_method_has_human_name_on_both_surfaces() -> None:
    """Название метода видно и в кабинете, и в админке бота.

    У обеих поверхностей свой список названий; забытая запись даёт не ошибку,
    а сырое ``platega_recurrent`` в интерфейсе.
    """
    from app.handlers.admin.payments import _method_display
    from app.services.payment_verification_service import method_display_name

    cabinet = method_display_name(PaymentMethod.PLATEGA_RECURRENT)
    bot = _method_display(PaymentMethod.PLATEGA_RECURRENT)

    assert cabinet == bot
    assert 'СБП' in cabinet
    assert PaymentMethod.PLATEGA_RECURRENT.value not in cabinet
