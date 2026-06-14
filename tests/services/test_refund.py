import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.database.crud.achievement as ach_crud
import app.database.crud.transaction as txn_crud
from app.database.crud.transaction import mark_transaction_refunded, unmark_transaction_refunded


def _txn():
    return SimpleNamespace(id=7, is_refunded=False, refunded_at=None, refunded_by=None)


class _DB:
    def __init__(self, txn):
        self._txn = txn
        self.commit = AsyncMock()

    async def get(self, model, pk):
        return self._txn if (self._txn and self._txn.id == pk) else None


@pytest.mark.asyncio
async def test_mark_transaction_refunded_sets_fields():
    txn = _txn()
    db = _DB(txn)
    out = await mark_transaction_refunded(db, 7, admin_id=42)
    assert out is txn
    assert txn.is_refunded is True
    assert txn.refunded_by == 42
    assert isinstance(txn.refunded_at, datetime)
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_unmark_transaction_refunded_clears_fields():
    txn = SimpleNamespace(id=7, is_refunded=True, refunded_at=datetime.now(UTC), refunded_by=42)
    db = _DB(txn)
    out = await unmark_transaction_refunded(db, 7)
    assert out is txn
    assert txn.is_refunded is False
    assert txn.refunded_at is None
    assert txn.refunded_by is None


@pytest.mark.asyncio
async def test_mark_missing_transaction_returns_none():
    db = _DB(None)
    assert await mark_transaction_refunded(db, 999, admin_id=1) is None


def test_transaction_stats_exclude_refunded():
    for fn in (txn_crud.get_transactions_statistics, txn_crud.get_user_total_spent_kopeks):
        assert 'is_refunded' in inspect.getsource(fn), f'{fn.__name__} must exclude refunded'


def test_achievement_money_stats_exclude_refunded():
    src = inspect.getsource(ach_crud._get_user_stat)
    # the 4 money branches (total_spent / topup_count / single_topup_max / referral_revenue)
    assert src.count('is_refunded') >= 4, 'all 4 money branches must exclude refunded'
