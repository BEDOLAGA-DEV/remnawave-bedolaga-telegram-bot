"""Гард от дубля рекуррент-списания на стыке суток.

Ключ идемпотентности содержит календарную дату (``recurrent_{sub}_{card}_{Y-m-d}``)
и уходит в ЭП как ``payment_id``, поэтому шлюз схлопывает повторы только внутри
одних суток. Если charge ушёл в 23:5x, а вебхук задержался, следующий проход
после полуночи сгенерил бы новый ``payment_id`` и списал второй раз.
``get_unresolved_recurrent_payment`` держит проход, пока попытка в ``pending``.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud.etoplatezhi import get_unresolved_recurrent_payment
from app.database.models import EtoplatezhiPayment
from tests.fixtures.sqlite_memory import memory_session


TABLES = (EtoplatezhiPayment.__table__,)


async def _add_payment(
    db,
    *,
    order_id: str,
    status: str = 'pending',
    is_paid: bool = False,
    age: timedelta = timedelta(minutes=5),
) -> EtoplatezhiPayment:
    payment = EtoplatezhiPayment(
        user_id=1,
        order_id=order_id,
        amount_kopeks=29900,
        currency='RUB',
        status=status,
        is_paid=is_paid,
        created_at=datetime.now(UTC) - age,
    )
    db.add(payment)
    await db.commit()
    return payment


async def test_pending_attempt_blocks_next_charge(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _add_payment(db, order_id='recurrent_777_42_2026-08-08')

        found = await get_unresolved_recurrent_payment(db, 777)

        assert found is not None, 'висящий pending обязан блокировать повторный charge'


async def test_resolved_attempt_does_not_block(monkeypatch):
    """Отказ/успех — терминальные, следующий проход должен идти как обычно."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _add_payment(db, order_id='recurrent_777_42_2026-08-08', status='declined')
        await _add_payment(db, order_id='recurrent_777_43_2026-08-08', status='success', is_paid=True)

        assert await get_unresolved_recurrent_payment(db, 777) is None


async def test_stale_pending_expires_from_window(monkeypatch):
    """Навсегда зависший pending не должен вечно блокировать продления."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _add_payment(db, order_id='recurrent_777_42_2026-07-10', age=timedelta(hours=30))

        assert await get_unresolved_recurrent_payment(db, 777) is None
        assert await get_unresolved_recurrent_payment(db, 777, within_hours=72) is not None


async def test_subscription_id_prefix_is_not_matched(monkeypatch):
    """``_`` в LIKE — подстановочный символ: без экранирования подписка 777
    подхватила бы pending от 7770 и молча пропустила своё списание."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _add_payment(db, order_id='recurrent_7770_42_2026-08-08')

        assert await get_unresolved_recurrent_payment(db, 777) is None
        assert await get_unresolved_recurrent_payment(db, 7770) is not None
