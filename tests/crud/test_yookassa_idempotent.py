"""
Регрессия: ``create_yookassa_payment`` обязан быть идемпотентным по
``yookassa_payment_id``.

Рекуррентный автоплатёж (``recurrent_payment_service``) использует
детерминированный ключ идемпотентности на (подписку, карту, день), поэтому
повторные запуски шедулера в тот же день получают от YooKassa ТОТ ЖЕ
``payment_id``. Раньше повторная вставка била по unique-индексу
``ix_yookassa_payments_yookassa_payment_id`` и логировалась как ложный
«FK violation … user_id не существует» на уровне ERROR, заваливая админ-чат
(прод-отчёт от 01.06.2026, payment 31aee339-000f-5000-b000-1d3e737c34d1).
"""

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import IntegrityError

from app.database.crud import yookassa as yk


def _result(value):
    """Мок результата ``db.execute``: ``.scalar_one_or_none()`` (sync) -> value."""
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    return res


def _db(execute_returns):
    """Мок AsyncSession; ``db.execute`` последовательно отдаёт переданные значения
    (каждое заворачивается в результат с ``scalar_one_or_none``)."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(v) for v in execute_returns])
    return db


def _integrity_error() -> IntegrityError:
    return IntegrityError('INSERT', {}, Exception('duplicate key value violates unique constraint'))


async def test_returns_existing_without_insert_on_duplicate():
    """Запись с таким payment_id уже есть → вернуть её, НЕ вставлять повторно."""
    existing = MagicMock(yookassa_payment_id='dup-1')
    db = _db([existing])  # пре-чек находит запись

    result = await yk.create_yookassa_payment(
        db=db,
        user_id=607,
        yookassa_payment_id='dup-1',
        amount_kopeks=7420,
        currency='RUB',
        description='RichVPN',
        status='canceled',
    )

    assert result is existing
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_inserts_when_new():
    """Записи нет → создаём, коммитим, возвращаем новый платёж."""
    db = _db([None])  # пре-чек пуст

    result = await yk.create_yookassa_payment(
        db=db,
        user_id=607,
        yookassa_payment_id='new-1',
        amount_kopeks=7420,
        currency='RUB',
        description='RichVPN',
        status='pending',
    )

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once()
    assert result is not None
    assert result.yookassa_payment_id == 'new-1'


async def test_idempotent_on_insert_race(monkeypatch):
    """Пре-чек пуст, но коммит упал по дубликату (гонка) → вернуть запись
    конкурента, без ERROR в лог."""
    winner = MagicMock(yookassa_payment_id='race-1')
    db = _db([None, winner])  # 1) пре-чек None, 2) пере-чтение после rollback
    db.commit = AsyncMock(side_effect=_integrity_error())
    spy_logger = MagicMock()
    monkeypatch.setattr(yk, 'logger', spy_logger)

    result = await yk.create_yookassa_payment(
        db=db,
        user_id=607,
        yookassa_payment_id='race-1',
        amount_kopeks=100,
        currency='RUB',
        description='RichVPN',
        status='pending',
    )

    assert result is winner
    db.rollback.assert_awaited_once()
    spy_logger.error.assert_not_called()


async def test_real_integrity_error_returns_none_and_logs(monkeypatch):
    """Не дубликат (например, настоящая FK по user_id) → None и корректный ERROR."""
    db = _db([None, None])  # пре-чек пуст и после rollback записи нет
    db.commit = AsyncMock(side_effect=_integrity_error())
    spy_logger = MagicMock()
    monkeypatch.setattr(yk, 'logger', spy_logger)

    result = await yk.create_yookassa_payment(
        db=db,
        user_id=999999,
        yookassa_payment_id='ghost-1',
        amount_kopeks=100,
        currency='RUB',
        description='RichVPN',
        status='pending',
    )

    assert result is None
    db.rollback.assert_awaited_once()
    spy_logger.error.assert_called_once()
