"""Петля 3041: повтор с тем же payment_id вместо списания.

ЭП требует уникальный `payment_id` в рамках проекта. Раньше ключ был
`recurrent_{подписка}_{карта}_{дата}` и переиспользовался весь день: первая
попытка ключ «съедала», а каждая следующая получала отказ 3041
«Payment ID already exists» — денег никто не списывал, зато статус строки
перезаписывался безликим `error` поверх настоящей причины (нет средств,
карта заблокирована). На проде так было перезаписано 5741 из 5745 «ошибок».

Теперь каждая попытка получает свой ключ (`…r2`, `…r3`) и свою строку.
Обратная сторона: свежий ключ — это честный НОВЫЙ платёж, поэтому чеканить
его можно только там, где точно известно, что деньги не списались.
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
    _is_ambiguous_charge_failure,
    _next_attempt_key,
)
from tests.fixtures.sqlite_memory import memory_session


BASE = 'recurrent_777_42_2026-08-09'
TABLES = (EtoplatezhiPayment.__table__,)


class _Row:
    """Минимальная строка платежа: резолвер читает status/is_paid/order_id."""

    def __init__(self, status: str, order_id: str, *, is_paid: bool = False) -> None:
        self.status = status
        self.order_id = order_id
        self.is_paid = is_paid


def _rows(*statuses: str) -> list[_Row]:
    """Попытки, занявшие ключи по порядку — как оно идёт в жизни."""
    ids = _attempt_order_ids(BASE)
    return [_Row(status, ids[i]) for i, status in enumerate(statuses)]


# --- выбор ключа попытки -----------------------------------------------------


def test_first_attempt_uses_base_key() -> None:
    assert _next_attempt_key(BASE, []) == (BASE, 'first')


def test_retry_after_decline_gets_fresh_key() -> None:
    """Ключевой случай: явный отказ банка — повтор идёт с НОВЫМ payment_id."""
    key, reason = _next_attempt_key(BASE, _rows('declined'))
    assert key == f'{BASE}r2', 'повтор с тем же ключом словил бы 3041 вместо списания'
    assert reason == 'retry'


def test_retry_after_error_gets_fresh_key() -> None:
    key, _ = _next_attempt_key(BASE, _rows('declined', 'error'))
    assert key == f'{BASE}r3'


def test_paid_blocks_further_charges() -> None:
    assert _next_attempt_key(BASE, _rows('success')) == (None, 'paid')
    assert _next_attempt_key(BASE, _rows('declined', 'success')) == (None, 'paid')


def test_is_paid_flag_blocks_even_with_odd_status() -> None:
    rows = [_Row('weird', BASE, is_paid=True)]
    assert _next_attempt_key(BASE, rows) == (None, 'paid')


def test_settled_statuses_block_retry() -> None:
    """refunded/reversed = списание СОСТОЯЛОСЬ, повтор запрещён.

    is_paid у таких строк False, и без явной проверки статуса они выглядели бы
    обычной потраченной попыткой — резолвер выдал бы свежий ключ и списал ещё раз.
    """
    for status in ('refunded', 'partially_refunded', 'reversed'):
        assert _next_attempt_key(BASE, _rows(status)) == (None, 'paid'), status


def test_inflight_pending_blocks_further_charges() -> None:
    """Пока попытка в полёте — ждём вебхук, второй charge был бы дублем."""
    assert _next_attempt_key(BASE, _rows('pending')) == (None, 'inflight')
    assert _next_attempt_key(BASE, _rows('declined', 'pending')) == (None, 'inflight')


def test_daily_attempt_limit_is_enforced() -> None:
    rows = _rows(*['declined'] * MAX_DAILY_CHARGE_ATTEMPTS)
    assert _next_attempt_key(BASE, rows) == (None, 'exhausted')


def test_keys_never_repeat_across_a_day() -> None:
    """Прогон по всем попыткам суток: ни один ключ не повторяется."""
    rows: list[_Row] = []
    seen: list[str] = []
    while True:
        key, _ = _next_attempt_key(BASE, rows)
        if key is None:
            break
        assert key not in seen, f'ключ {key} выдан повторно → 3041'
        seen.append(key)
        rows.append(_Row('declined', key))
    assert len(seen) == MAX_DAILY_CHARGE_ATTEMPTS


def test_key_choice_survives_a_hole_in_attempts() -> None:
    """Базовую строку удалили вручную, r2 осталась — занятый r2 предлагать нельзя."""
    key, _ = _next_attempt_key(BASE, [_Row('declined', f'{BASE}r2')])
    assert key == BASE, 'счёт по длине списка предложил бы занятый r2 → unique violation'


def test_key_choice_picks_first_free_slot() -> None:
    rows = [_Row('error', BASE), _Row('error', f'{BASE}r3')]
    assert _next_attempt_key(BASE, rows) == (f'{BASE}r2', 'retry')


def test_attempt_order_ids_cover_every_possible_key() -> None:
    """Выборка из БД обязана покрывать все ключи, что умеет выдавать резолвер."""
    ids = _attempt_order_ids(BASE)
    assert len(ids) == MAX_DAILY_CHARGE_ATTEMPTS
    assert ids[0] == BASE

    rows: list[_Row] = []
    while True:
        key, _ = _next_attempt_key(BASE, rows)
        if key is None:
            break
        assert key in ids, f'резолвер выдал ключ {key}, которого нет в выборке'
        rows.append(_Row('error', key))


def test_unknown_status_counts_as_failed_attempt() -> None:
    """Незнакомый статус — не оплачено и не в полёте, значит попытка потрачена."""
    assert _next_attempt_key(BASE, _rows('weird')) == (f'{BASE}r2', 'retry')


# --- неоднозначные отказы ----------------------------------------------------


def test_ambiguous_failures_are_recognised() -> None:
    """Таймаут/обрыв/5xx: запрос мог быть исполнен, а ответ потерян."""
    for code in ('http_error', 'http_500', 'http_502', 'HTTP_503'):
        assert _is_ambiguous_charge_failure(code) is True, code


def test_unambiguous_failures_are_not_treated_as_ambiguous() -> None:
    """Явный отказ шлюза и отбойные 4xx — денег точно не списали."""
    for code in ('charge_declined', 'invalid_token', 'no_customer_id', 'http_400', 'http_404', None, ''):
        assert _is_ambiguous_charge_failure(code) is False, code


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
