"""Петля 3041: повтор с тем же payment_id вместо списания.

ЭП требует уникальный `payment_id` в рамках проекта. Раньше ключ был
`recurrent_{подписка}_{карта}_{дата}` и переиспользовался весь день: первая
попытка ключ «съедала», а каждая следующая получала отказ 3041
«Payment ID already exists» — денег никто не списывал, зато статус строки
перезаписывался безликим `error` поверх настоящей причины (нет средств,
карта заблокирована). На проде так было перезаписано 5741 из 5745 «ошибок».

Теперь каждая попытка получает свой ключ (`…r2`, `…r3`) и свою строку.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud.etoplatezhi import get_recurrent_attempts
from app.database.models import EtoplatezhiPayment
from app.services.recurrent_payment_service import (
    MAX_DAILY_CHARGE_ATTEMPTS,
    _attempt_order_ids,
    _next_attempt_key,
)
from tests.fixtures.sqlite_memory import memory_session


BASE = 'recurrent_777_42_2026-08-09'
TABLES = (EtoplatezhiPayment.__table__,)


class _Row:
    """Минимальная строка платежа — _next_attempt_key читает только status/is_paid."""

    def __init__(self, status: str, is_paid: bool = False) -> None:
        self.status = status
        self.is_paid = is_paid


# --- чистая логика выбора ключа ---------------------------------------------


def test_first_attempt_uses_base_key() -> None:
    assert _next_attempt_key(BASE, []) == (BASE, 'first')


def test_retry_after_decline_gets_fresh_key() -> None:
    """Ключевой случай: отказ банка — повтор обязан идти с НОВЫМ payment_id."""
    key, reason = _next_attempt_key(BASE, [_Row('declined')])
    assert key == f'{BASE}r2', 'повтор с тем же ключом словил бы 3041 вместо списания'
    assert reason == 'retry'


def test_retry_after_error_gets_fresh_key() -> None:
    key, _ = _next_attempt_key(BASE, [_Row('declined'), _Row('error')])
    assert key == f'{BASE}r3'


def test_paid_blocks_further_charges() -> None:
    """Оплаченное не трогаем — иначе дубль списания."""
    assert _next_attempt_key(BASE, [_Row('success', is_paid=True)]) == (None, 'paid')
    assert _next_attempt_key(BASE, [_Row('declined'), _Row('success')]) == (None, 'paid')


def test_inflight_pending_blocks_further_charges() -> None:
    """Пока попытка в полёте — ждём вебхук, второй charge был бы дублем."""
    assert _next_attempt_key(BASE, [_Row('pending')]) == (None, 'inflight')
    assert _next_attempt_key(BASE, [_Row('declined'), _Row('pending')]) == (None, 'inflight')


def test_daily_attempt_limit_is_enforced() -> None:
    rows = [_Row('declined') for _ in range(MAX_DAILY_CHARGE_ATTEMPTS)]
    assert _next_attempt_key(BASE, rows) == (None, 'exhausted')


def test_keys_never_repeat_across_a_day() -> None:
    """Прогон по всем попыткам суток: ни один ключ не повторяется."""
    rows: list[_Row] = []
    seen: list[str] = []
    while True:
        key, _ = _next_attempt_key(BASE, rows)
        if key is None:
            break
        seen.append(key)
        rows.append(_Row('declined'))
    assert len(seen) == MAX_DAILY_CHARGE_ATTEMPTS
    assert len(set(seen)) == len(seen), f'повтор ключа → 3041: {seen}'


def test_attempt_order_ids_cover_every_possible_key() -> None:
    """Выборка из БД должна покрывать ровно те ключи, что умеет выдавать резолвер."""
    ids = _attempt_order_ids(BASE)
    assert len(ids) == MAX_DAILY_CHARGE_ATTEMPTS
    assert ids[0] == BASE

    rows: list[_Row] = []
    while True:
        key, _ = _next_attempt_key(BASE, rows)
        if key is None:
            break
        assert key in ids, f'резолвер выдал ключ {key}, которого нет в выборке'
        rows.append(_Row('error'))


def test_unknown_status_counts_as_failed_attempt() -> None:
    """Незнакомый статус — не оплачено и не в полёте, значит попытка потрачена."""
    key, reason = _next_attempt_key(BASE, [_Row('weird')])
    assert key == f'{BASE}r2'
    assert reason == 'retry'


# --- выборка из БД -----------------------------------------------------------


async def _add(db, order_id: str, status: str = 'declined') -> None:
    db.add(
        EtoplatezhiPayment(
            user_id=1,
            order_id=order_id,
            amount_kopeks=29900,
            currency='RUB',
            status=status,
            is_paid=False,
            created_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db.commit()


async def test_get_recurrent_attempts_picks_up_suffixed_rows(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _add(db, BASE)
        await _add(db, f'{BASE}r2')

        rows = await get_recurrent_attempts(db, _attempt_order_ids(BASE))

        assert [r.order_id for r in rows] == [BASE, f'{BASE}r2']


async def test_get_recurrent_attempts_ignores_other_cards_and_days(monkeypatch):
    """`_` в ключе не должен работать как подстановочный символ."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _add(db, BASE)
        await _add(db, 'recurrent_777_43_2026-08-09')  # другая карта
        await _add(db, 'recurrent_777_42_2026-08-08')  # другой день
        await _add(db, 'recurrent_7770_42_2026-08-09')  # другая подписка

        rows = await get_recurrent_attempts(db, _attempt_order_ids(BASE))

        assert [r.order_id for r in rows] == [BASE]


async def test_get_recurrent_attempts_empty_input(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        assert await get_recurrent_attempts(db, []) == []
