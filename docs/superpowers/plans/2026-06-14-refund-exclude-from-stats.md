# Refund marking + stats exclusion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin mark a payment `Transaction` as refunded in the bot, and exclude every refunded transaction from all money statistics (balance/subscription/referrals untouched).

**Architecture:** Add `is_refunded`/`refunded_at`/`refunded_by` to `Transaction` (+ migration). Add an additive `is_refunded == False` AND-clause to every money aggregate. Add a bot admin user-card UI to mark/unmark a transaction refunded via two CRUD helpers.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, Alembic, aiogram 3, pytest (`.venv/Scripts/python.exe -m pytest`).

---

## File Structure

- `app/database/models.py` — 3 columns on `Transaction`.
- `migrations/alembic/versions/0116_add_transaction_refund_flag.py` — migration (down_revision = real `0115` head id).
- `app/database/crud/transaction.py` — `mark_transaction_refunded`/`unmark_transaction_refunded` + filters on `get_transactions_statistics`, `get_user_total_spent_kopeks`.
- `app/database/crud/achievement.py` — filters on 4 `_get_user_stat` money branches.
- `app/cabinet/routes/admin_stats.py`, `app/cabinet/routes/admin_sales_stats.py` — filters on revenue/addon/renewal aggregates.
- `app/handlers/admin/users.py` — refund buttons in `show_user_transactions` + handlers + registration.
- `tests/services/test_refund.py` — CRUD tests + source-inspection stat-filter tests.

Run tests: `.venv/Scripts/python.exe -m pytest <path> -q`. Commit after each task.

---

## Task 1: Model columns + migration

**Files:**
- Modify: `app/database/models.py` (`Transaction`, after `completed_at`)
- Create: `migrations/alembic/versions/0116_add_transaction_refund_flag.py`

- [ ] **Step 1: Add columns** to `Transaction` after `completed_at = Column(...)`:

```python
    is_refunded = Column(Boolean, default=False, nullable=False, server_default='false')
    refunded_at = Column(AwareDateTime(), nullable=True)
    refunded_by = Column(Integer, nullable=True)
```

- [ ] **Step 2: Create migration** `migrations/alembic/versions/0116_add_transaction_refund_flag.py`:

```python
"""add transaction refund flag

Revision ID: 0116
Revises: 0115
"""
from alembic import op
import sqlalchemy as sa

revision = '0116'
down_revision = '0115'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('transactions', sa.Column('is_refunded', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('transactions', sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('transactions', sa.Column('refunded_by', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('transactions', 'refunded_by')
    op.drop_column('transactions', 'refunded_at')
    op.drop_column('transactions', 'is_refunded')
```

(Open the existing `0115` migration file to confirm the `revision`/`down_revision` id format and match it exactly.)

- [ ] **Step 3: Verify migration graph is linear**

Run: `.venv/Scripts/python.exe -m alembic -c alembic.ini heads`
Expected: a single head = `0116`.

- [ ] **Step 4: Smoke-import the model**

Run: `.venv/Scripts/python.exe -c "from app.database.models import Transaction; print(Transaction.is_refunded)"`
Expected: prints a column attribute, no error.

- [ ] **Step 5: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/0116_add_transaction_refund_flag.py
git commit -m "feat(refund): add is_refunded/refunded_at/refunded_by to Transaction"
```

---

## Task 2: CRUD mark/unmark (TDD)

**Files:**
- Create: `tests/services/test_refund.py`
- Modify: `app/database/crud/transaction.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_refund.py
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_refund.py -q`
Expected: FAIL — import error (functions missing).

- [ ] **Step 3: Implement** in `app/database/crud/transaction.py` (append near other helpers):

```python
async def mark_transaction_refunded(db: AsyncSession, transaction_id: int, admin_id: int) -> Transaction | None:
    """Mark a transaction refunded so it drops out of all money statistics. Idempotent."""
    txn = await db.get(Transaction, transaction_id)
    if txn is None:
        return None
    txn.is_refunded = True
    txn.refunded_at = datetime.now(UTC)
    txn.refunded_by = admin_id
    await db.commit()
    logger.info('Transaction marked refunded', transaction_id=transaction_id, admin_id=admin_id)
    return txn


async def unmark_transaction_refunded(db: AsyncSession, transaction_id: int) -> Transaction | None:
    """Undo a refund mark (admin mistake recovery)."""
    txn = await db.get(Transaction, transaction_id)
    if txn is None:
        return None
    txn.is_refunded = False
    txn.refunded_at = None
    txn.refunded_by = None
    await db.commit()
    logger.info('Transaction refund mark cleared', transaction_id=transaction_id)
    return txn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_refund.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/database/crud/transaction.py tests/services/test_refund.py
git commit -m "feat(refund): mark/unmark transaction refunded CRUD"
```

---

## Task 3: Exclude refunded from every money statistic

Add `Transaction.is_refunded.is_(False)` to each aggregate's `and_(...)`/`where(...)`.

**Files:**
- Modify: `app/database/crud/transaction.py` — `get_transactions_statistics` (income, ~line 350), `get_user_total_spent_kopeks` (~line 296).
- Modify: `app/database/crud/achievement.py` — `_get_user_stat`: `total_spent_kopeks` (~227), `topup_count` (~311), `single_topup_max_kopeks` (~364), `referral_revenue_kopeks` (~400).
- Modify: `app/cabinet/routes/admin_stats.py` — `get_transactions_statistics` revenue block.
- Modify: `app/cabinet/routes/admin_sales_stats.py` — total revenue (~117), addon revenue (~248), renewal-count sums keyed on `SUBSCRIPTION_PAYMENT` (~227/237), and any other `Transaction` money sum keyed on DEPOSIT/SUBSCRIPTION_PAYMENT.

NOTE (deliberate boundary): leave the anti-farm "did they ever pay" gates (`days_active` paid_check ~252, `referral_count` paid_refs_subq ~270) UNCHANGED — those are unlock gates, not statistics; changing them is out of the approved scope.

- [ ] **Step 1: Write source-inspection tests** (append to `tests/services/test_refund.py`) — these lock the filter into each function so a future edit can't silently drop it:

```python
import inspect

import app.database.crud.transaction as txn_crud
import app.database.crud.achievement as ach_crud


def test_transaction_stats_exclude_refunded():
    for fn in (txn_crud.get_transactions_statistics, txn_crud.get_user_total_spent_kopeks):
        assert 'is_refunded' in inspect.getsource(fn), f'{fn.__name__} must exclude refunded'


def test_achievement_money_stats_exclude_refunded():
    src = inspect.getsource(ach_crud._get_user_stat)
    # the 4 money branches (total_spent / topup_count / single_topup_max / referral_revenue)
    assert src.count('is_refunded') >= 4, 'all 4 money branches must exclude refunded'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_refund.py -q`
Expected: FAIL on the two new tests.

- [ ] **Step 3: Add the filter** to `get_transactions_statistics` income query (`transaction.py`):

```python
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                Transaction.is_refunded.is_(False),
```

and to `get_user_total_spent_kopeks`:

```python
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_refunded.is_(False),
```

- [ ] **Step 4: Add the filter** to the 4 `_get_user_stat` money branches in `achievement.py` — append `Transaction.is_refunded.is_(False),` inside each `and_(...)`:
  - `total_spent_kopeks` (the sum at ~227)
  - `topup_count` (the count at ~311)
  - `single_topup_max_kopeks` (the max at ~364)
  - `referral_revenue_kopeks` (the sum at ~400)

- [ ] **Step 5: Add the filter** to `admin_stats.py` revenue block and `admin_sales_stats.py` total-revenue (~117), addon-revenue (~248), and renewal-count sums (~227/237). For each `select(...).where(and_(...))` that sums/counts `Transaction` by DEPOSIT/SUBSCRIPTION_PAYMENT, add `Transaction.is_refunded.is_(False),`. (Open each file, find every `Transaction.type ==`/`Transaction.type.in_(` money aggregate, add the clause. Do NOT touch the MANUAL top-up sum.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_refund.py -q`
Expected: PASS.

- [ ] **Step 7: Run the achievement regression suite (no behaviour break when nothing refunded)**

Run: `.venv/Scripts/python.exe -m pytest tests/regression tests/services/test_achievements_sweep.py -q -k "achievement or sweep or refund"`
Expected: all PASS.

- [ ] **Step 8: Smoke-import all touched modules**

Run: `.venv/Scripts/python.exe -c "import app.database.crud.transaction, app.database.crud.achievement, app.cabinet.routes.admin_stats, app.cabinet.routes.admin_sales_stats"`
Expected: no error.

- [ ] **Step 9: Commit**

```bash
git add app/database/crud/transaction.py app/database/crud/achievement.py app/cabinet/routes/admin_stats.py app/cabinet/routes/admin_sales_stats.py tests/services/test_refund.py
git commit -m "feat(refund): exclude refunded transactions from all money statistics"
```

---

## Task 4: Bot admin UI — mark/unmark refund in the user card

**Files:**
- Modify: `app/handlers/admin/users.py` — `show_user_transactions` (~line 1076): render a refund button per real payment; add `admin_txn_refund` / `admin_txn_unrefund` handlers; register them (~line 6733).

- [ ] **Step 1: Read `show_user_transactions`** (`app/handlers/admin/users.py:1076`) and the transaction-row rendering to learn the existing layout and how `user_id` is parsed from `callback.data`.

- [ ] **Step 2: Render a refund toggle per real payment.** For each transaction that is `DEPOSIT` or `SUBSCRIPTION_PAYMENT`, add an inline button to the keyboard:

```python
        is_real = t.type in (TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value)
        if is_real:
            if getattr(t, 'is_refunded', False):
                rows.append([types.InlineKeyboardButton(
                    text=f'↩️ Отменить возврат #{t.id}', callback_data=f'admin_txn_unrefund_{t.id}_{user_id}')])
            else:
                rows.append([types.InlineKeyboardButton(
                    text=f'↩️ Возврат #{t.id}', callback_data=f'admin_txn_refund_{t.id}_{user_id}')])
```

Also prefix refunded rows in the text with `↩️` so the admin sees state. (Adapt variable names to the actual loop in the function.)

- [ ] **Step 3: Add handlers** (near `show_user_transactions`):

```python
@admin_required
@error_handler
async def admin_txn_refund(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.database.crud.transaction import mark_transaction_refunded

    parts = callback.data.split('_')  # admin_txn_refund_<txn>_<user>
    txn_id, user_id = int(parts[3]), int(parts[4])
    await mark_transaction_refunded(db, txn_id, admin_id=db_user.id)
    await callback.answer('↩️ Помечено возвратом (исключено из статистики)', show_alert=True)
    callback.data = f'admin_user_transactions_{user_id}'
    await show_user_transactions(callback, db_user, db)


@admin_required
@error_handler
async def admin_txn_unrefund(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    from app.database.crud.transaction import unmark_transaction_refunded

    parts = callback.data.split('_')
    txn_id, user_id = int(parts[3]), int(parts[4])
    await unmark_transaction_refunded(db, txn_id)
    await callback.answer('Возврат отменён', show_alert=True)
    callback.data = f'admin_user_transactions_{user_id}'
    await show_user_transactions(callback, db_user, db)
```

(Match the decorator/argument signature of the sibling handlers in this file exactly — check `show_user_transactions`'s signature; if it takes `state`, include it.)

- [ ] **Step 4: Register** near line 6733:

```python
    dp.callback_query.register(admin_txn_refund, F.data.startswith('admin_txn_refund_'))
    dp.callback_query.register(admin_txn_unrefund, F.data.startswith('admin_txn_unrefund_'))
```

Ensure the `admin_user_transactions_` registration's filter does not also match `admin_txn_*` (different prefix — safe).

- [ ] **Step 5: Smoke-import**

Run: `.venv/Scripts/python.exe -c "import app.handlers.admin.users"`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add app/handlers/admin/users.py
git commit -m "feat(refund): bot admin UI to mark/unmark a transaction refunded"
```

---

## Task 5: Full verification

- [ ] **Step 1: Run refund + achievement + regression tests**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_refund.py tests/regression -q -k "refund or achievement"`
Expected: all PASS.

- [ ] **Step 2: Alembic single head**

Run: `.venv/Scripts/python.exe -m alembic -c alembic.ini heads`
Expected: single head `0116`.

- [ ] **Step 3: Smoke-import everything touched**

Run: `.venv/Scripts/python.exe -c "import app.database.models, app.database.crud.transaction, app.database.crud.achievement, app.cabinet.routes.admin_stats, app.cabinet.routes.admin_sales_stats, app.handlers.admin.users"`
Expected: no error.

- [ ] **Step 4: Final review** — confirm: 3 columns + migration; mark/unmark CRUD; `is_refunded` filter in every listed money aggregate (revenue, sales, addon, renewals, user-spent, 4 achievement branches); bot UI mark/unmark toggle + handlers registered; balance/subscription/referrals untouched; anti-farm paid-gates intentionally unchanged.

---

## Self-review notes

- **Spec coverage:** model+migration (T1), CRUD (T2), stats exclusion across all listed sites (T3), bot admin UI (T4), tests (T2/T3). ✅
- **Names consistent:** `is_refunded`/`refunded_at`/`refunded_by`, `mark_transaction_refunded`/`unmark_transaction_refunded`, `admin_txn_refund`/`admin_txn_unrefund`. ✅
- **Out of scope honored:** no balance/subscription/referral mutation; no provider API; cabinet UI not added; auto-flows not auto-marked. ✅
- **Risk:** exact line numbers in admin_stats/admin_sales_stats may differ — anchor on `Transaction.type` money aggregates, not line numbers. Match the migration down_revision to the real `0115` file's revision id/format.
