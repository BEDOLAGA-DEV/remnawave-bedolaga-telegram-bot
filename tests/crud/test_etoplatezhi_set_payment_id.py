"""Tests for set_etoplatezhi_payment_id_if_missing — race-safe partial UPDATE."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database.crud.etoplatezhi import set_etoplatezhi_payment_id_if_missing


def _make_db(rowcount: int = 1):
    db = SimpleNamespace()
    result = SimpleNamespace(rowcount=rowcount)
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


async def test_returns_rowcount_when_updated():
    db = _make_db(rowcount=1)
    n = await set_etoplatezhi_payment_id_if_missing(
        db, order_id='recurrent_42_7_2026-05-26', etoplatezhi_payment_id='99109010223425'
    )
    assert n == 1
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()


async def test_returns_zero_when_already_set():
    db = _make_db(rowcount=0)
    n = await set_etoplatezhi_payment_id_if_missing(
        db, order_id='recurrent_42_7_2026-05-26', etoplatezhi_payment_id='99109010223425'
    )
    assert n == 0


async def test_rowcount_none_is_treated_as_zero():
    db = _make_db(rowcount=None)
    n = await set_etoplatezhi_payment_id_if_missing(db, order_id='x', etoplatezhi_payment_id='y')
    assert n == 0


async def test_update_filters_by_order_id_and_null_payment_id():
    """UPDATE must include both WHERE conditions, so a webhook-set non-null
    payment_id is never overwritten — and we never touch status/is_paid."""
    db = _make_db(rowcount=1)
    await set_etoplatezhi_payment_id_if_missing(db, order_id='abc', etoplatezhi_payment_id='xyz')

    sent_stmt = db.execute.call_args.args[0]
    compiled = str(sent_stmt.compile(compile_kwargs={'literal_binds': True}))

    assert 'etoplatezhi_payments' in compiled.lower()
    assert 'etoplatezhi_payment_id' in compiled.lower()
    assert 'order_id' in compiled.lower()
    assert 'is null' in compiled.lower()
    assert 'status' not in compiled.split('SET')[1].lower()
    assert 'is_paid' not in compiled.split('SET')[1].lower()
