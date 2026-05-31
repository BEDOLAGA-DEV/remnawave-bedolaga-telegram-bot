# Реферальные милстоуны Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Накопительные награды за N оплативших рефералов (admin-лесенка порог→награда balance/promo_group), поверх существующей рефералки, идемпотентно, за env-флагом (дефолт OFF).

**Architecture:** `ReferralMilestone` (admin CMS) + `UserReferralMilestoneClaim` (idempotency, unique(user,milestone)). Метрика = `count_paid_referrals` (DISTINCT referral_id с earning>0). `ReferralMilestoneService.reward_milestones` вызывается хуком в `process_referral_topup` после начисления, выдаёт невыданные милстоуны ≤ count. Показ прогресса бот+кабинет. Миграция 0100.

**Tech Stack:** Python 3.12, FastAPI, aiogram, SQLAlchemy async, Alembic; React/TS (nested cabinet repo); pytest.

**Spec:** `docs/superpowers/specs/2026-05-31-referral-milestones-design.md`

**Run tests:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `migrations/alembic/versions/0100_create_referral_milestones.py` + `app/database/models.py` — 2 tables + models (Task 1).
- `app/database/crud/referral.py` (+`count_paid_referrals`) + `app/database/crud/referral_milestone.py` (Task 2).
- `app/services/referral_milestone_service.py` — service + singleton (Task 3).
- `app/services/referral_service.py` — hook in `process_referral_topup` (Task 4).
- `app/config.py` — `REFERRAL_MILESTONES_ENABLED` (Task 2).
- `app/cabinet/routes/admin_referral_milestones.py` + cabinet progress endpoint + `__init__.py` (Task 5).
- `app/handlers/referral.py` — bot progress line (Task 6).
- nested cabinet repo: Referral.tsx milestone block (Task 7).

Tests: `tests/services/test_referral_milestone_service.py` (Tasks 2-3).

---

## Task 1: migration + models

**Files:**
- Create: `migrations/alembic/versions/0100_create_referral_milestones.py`
- Modify: `app/database/models.py`

**Context:** Latest head = `0099`. Mirror JSONB usage from `PartnerPromo`/`InfoPage`. `ReferralEarning` model already exists.

- [ ] **Step 1: Add models**

In `app/database/models.py` (near other referral models / end of file), add:
```python
class ReferralMilestone(Base):
    """Admin-defined milestone: reward for reaching N paid referrals."""

    __tablename__ = 'referral_milestones'
    id = Column(Integer, primary_key=True, index=True)
    threshold = Column(Integer, nullable=False, unique=True)        # N paid referrals
    reward_type = Column(String(20), nullable=False)                # 'balance' | 'promo_group'
    reward_value = Column(Integer, nullable=False)                  # kopeks (balance) | promo_group_id
    title = Column(JSONB, nullable=False, server_default='{}')      # multilingual
    is_active = Column(Boolean, nullable=False, default=True, server_default='true')
    created_at = Column(AwareDateTime(), server_default=func.now())


class UserReferralMilestoneClaim(Base):
    """Idempotency record: one claim per (user, milestone)."""

    __tablename__ = 'user_referral_milestone_claims'
    __table_args__ = (UniqueConstraint('user_id', 'milestone_id', name='uq_user_milestone_claim'),)
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    milestone_id = Column(Integer, ForeignKey('referral_milestones.id', ondelete='CASCADE'), nullable=False)
    claimed_at = Column(AwareDateTime(), server_default=func.now())
```
Confirm `JSONB`, `UniqueConstraint`, `ForeignKey`, `AwareDateTime`, `func` are imported (they are — used by existing models).

- [ ] **Step 2: Create migration**

Create `migrations/alembic/versions/0100_create_referral_milestones.py`:
```python
"""create referral milestones tables

Revision ID: 0100
Revises: 0099
Create Date: 2026-05-31

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0100'
down_revision: Union[str, None] = '0099'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'referral_milestones',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('threshold', sa.Integer(), nullable=False),
        sa.Column('reward_type', sa.String(20), nullable=False),
        sa.Column('reward_value', sa.Integer(), nullable=False),
        sa.Column('title', sa.dialects.postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('threshold', name='uq_referral_milestone_threshold'),
    )
    op.create_table(
        'user_referral_milestone_claims',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('milestone_id', sa.Integer(), sa.ForeignKey('referral_milestones.id', ondelete='CASCADE'), nullable=False),
        sa.Column('claimed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'milestone_id', name='uq_user_milestone_claim'),
    )


def downgrade() -> None:
    op.drop_table('user_referral_milestone_claims')
    op.drop_table('referral_milestones')
```
Confirm `0099` is the current single head. Match the JSONB accessor to how `0099_create_partner_promos.py` did it (`sa.dialects.postgresql.JSONB()` inline) — mirror exactly.

- [ ] **Step 3: Verify**

Run: `.venv/Scripts/python.exe -c "import app.database.models; print('models OK')"`
Run: `.venv/Scripts/python.exe -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print('heads:', s.get_heads())"` → single head `('0100',)`.

- [ ] **Step 4: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/0100_create_referral_milestones.py
git commit -m "feat(referral-milestones): tables + models (migration 0100)"
```

---

## Task 2: count metric + milestone CRUD + config

**Files:**
- Modify: `app/database/crud/referral.py`, `app/config.py`
- Create: `app/database/crud/referral_milestone.py`, `tests/services/test_referral_milestone_service.py`

**Context:** `ReferralEarning(user_id, referral_id, amount_kopeks, reason)`. `crud/referral.py` imports `select`, `func`, `and_`, `ReferralEarning`, `AsyncSession`.

- [ ] **Step 1: Add config**

In `app/config.py`, near other feature flags:
```python
    REFERRAL_MILESTONES_ENABLED: bool = False
```

- [ ] **Step 2: Write failing tests (count part)**

Create `tests/services/test_referral_milestone_service.py`:
```python
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.database.crud.referral as ref_crud


@pytest.mark.asyncio
async def test_count_paid_referrals_returns_scalar():
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = 5
    db.execute = AsyncMock(return_value=result)
    n = await ref_crud.count_paid_referrals(db, 42)
    assert n == 5
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_count_paid_referrals_none_to_zero():
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = None
    db.execute = AsyncMock(return_value=result)
    assert await ref_crud.count_paid_referrals(db, 42) == 0
```

- [ ] **Step 3: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_referral_milestone_service.py -v`

- [ ] **Step 4: Implement count metric**

In `app/database/crud/referral.py`, add near `get_commission_payment_count`:
```python
async def count_paid_referrals(db: AsyncSession, referrer_id: int) -> int:
    """DISTINCT referrals who actually paid (have a positive earning) — anti-fraud metric."""
    result = await db.execute(
        select(func.count(func.distinct(ReferralEarning.referral_id)))
        .where(ReferralEarning.user_id == referrer_id)
        .where(ReferralEarning.referral_id.isnot(None))
        .where(ReferralEarning.amount_kopeks > 0)
    )
    return int(result.scalar() or 0)
```

- [ ] **Step 5: Implement milestone CRUD**

Create `app/database/crud/referral_milestone.py`:
```python
from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ReferralMilestone, UserReferralMilestoneClaim


logger = structlog.get_logger(__name__)

_REWARD_TYPES = ('balance', 'promo_group')


async def list_active(db: AsyncSession) -> list[ReferralMilestone]:
    result = await db.execute(
        select(ReferralMilestone)
        .where(ReferralMilestone.is_active == True)  # noqa: E712
        .order_by(ReferralMilestone.threshold.asc())
    )
    return list(result.scalars().all())


async def list_all(db: AsyncSession) -> list[ReferralMilestone]:
    result = await db.execute(select(ReferralMilestone).order_by(ReferralMilestone.threshold.asc()))
    return list(result.scalars().all())


async def get(db: AsyncSession, milestone_id: int) -> ReferralMilestone | None:
    result = await db.execute(select(ReferralMilestone).where(ReferralMilestone.id == milestone_id))
    return result.scalar_one_or_none()


async def create(db: AsyncSession, *, threshold: int, reward_type: str, reward_value: int,
                 title: dict | None = None, is_active: bool = True) -> ReferralMilestone:
    if reward_type not in _REWARD_TYPES:
        raise ValueError(f'reward_type must be one of {_REWARD_TYPES}')
    if threshold < 1:
        raise ValueError('threshold must be >= 1')
    if reward_value < 0:
        raise ValueError('reward_value must be >= 0')
    m = ReferralMilestone(
        threshold=threshold, reward_type=reward_type, reward_value=reward_value,
        title=title or {}, is_active=is_active,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def update_milestone(db: AsyncSession, milestone_id: int, **fields) -> ReferralMilestone | None:
    if 'reward_type' in fields and fields['reward_type'] not in _REWARD_TYPES:
        raise ValueError(f'reward_type must be one of {_REWARD_TYPES}')
    m = await get(db, milestone_id)
    if m is None:
        return None
    for k, v in fields.items():
        if hasattr(m, k):
            setattr(m, k, v)
    await db.commit()
    await db.refresh(m)
    return m


async def delete(db: AsyncSession, milestone_id: int) -> bool:
    m = await get(db, milestone_id)
    if m is None:
        return False
    await db.delete(m)
    await db.commit()
    return True


async def get_claimed_milestone_ids(db: AsyncSession, user_id: int) -> set[int]:
    result = await db.execute(
        select(UserReferralMilestoneClaim.milestone_id).where(UserReferralMilestoneClaim.user_id == user_id)
    )
    return {row[0] for row in result.all()}
```

- [ ] **Step 6: Run → PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_referral_milestone_service.py -v`
Run: `.venv/Scripts/python.exe -c "import app.config; import app.database.crud.referral_milestone; print(app.config.settings.REFERRAL_MILESTONES_ENABLED)"` → `False`.

- [ ] **Step 7: Commit**

```bash
git add app/database/crud/referral.py app/database/crud/referral_milestone.py app/config.py tests/services/test_referral_milestone_service.py
git commit -m "feat(referral-milestones): paid-referral count metric + milestone CRUD + config"
```

---

## Task 3: ReferralMilestoneService

**Files:**
- Create: `app/services/referral_milestone_service.py`
- Modify: `tests/services/test_referral_milestone_service.py` (add service tests)

**Context:** `get_user_by_id` (crud/user). `add_user_balance(db, user, amount, description, transaction_type=..., commit=True, bot=None)` — supports `commit=False`. `add_user_to_promo_group(db, user_id, promo_group_id)` (crud/user_promo_group). `TransactionType.REFERRAL_REWARD`. CRUD from Task 2. Claim insert: catch `IntegrityError` (sqlalchemy.exc) for race idempotency.

- [ ] **Step 1: Add service tests**

Append to `tests/services/test_referral_milestone_service.py`:
```python
from types import SimpleNamespace

import app.services.referral_milestone_service as ms


def _milestone(mid, threshold, reward_type='balance', reward_value=5000):
    return SimpleNamespace(id=mid, threshold=threshold, reward_type=reward_type,
                           reward_value=reward_value, title={'ru': f'M{threshold}'})


@pytest.fixture
def service():
    svc = ms.ReferralMilestoneService()
    svc._notify = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(ms.settings, 'REFERRAL_MILESTONES_ENABLED', True, raising=False)
    yield


def _db():
    db = MagicMock()
    db.commit = AsyncMock(); db.rollback = AsyncMock(); db.add = MagicMock(); db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_disabled_returns_empty(service, monkeypatch):
    monkeypatch.setattr(ms.settings, 'REFERRAL_MILESTONES_ENABLED', False, raising=False)
    granted = await service.reward_milestones(_db(), 1, bot=None)
    assert granted == []


@pytest.mark.asyncio
async def test_grants_reached_unclaimed(service, monkeypatch):
    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=5))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 1), _milestone(2, 3), _milestone(3, 5), _milestone(4, 10)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value=set()))
    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr(ms, 'add_user_balance', add_balance)

    granted = await service.reward_milestones(db, 1, bot=None)

    assert len(granted) == 3  # thresholds 1,3,5 <= 5; 10 not
    assert add_balance.await_count == 3


@pytest.mark.asyncio
async def test_skips_already_claimed(service, monkeypatch):
    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=5))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 1), _milestone(3, 5)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value={1}))
    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr(ms, 'add_user_balance', add_balance)

    granted = await service.reward_milestones(db, 1, bot=None)
    assert len(granted) == 1  # only id=3
    add_balance.assert_awaited_once()


@pytest.mark.asyncio
async def test_promo_group_reward(service, monkeypatch):
    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=10))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 10, reward_type='promo_group', reward_value=7)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value=set()))
    add_pg = AsyncMock()
    monkeypatch.setattr(ms, 'add_user_to_promo_group', add_pg)

    granted = await service.reward_milestones(db, 1, bot=None)
    assert len(granted) == 1
    add_pg.assert_awaited_once_with(db, 1, 7)


@pytest.mark.asyncio
async def test_no_paid_referrals_noop(service, monkeypatch):
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(id=1, telegram_id=10, language='ru')))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=0))
    la = AsyncMock()
    monkeypatch.setattr(ms.milestone_crud, 'list_active', la)
    granted = await service.reward_milestones(_db(), 1, bot=None)
    assert granted == []
    la.assert_not_awaited()
```

- [ ] **Step 2: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_referral_milestone_service.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement service**

Create `app/services/referral_milestone_service.py`:
```python
from __future__ import annotations

import structlog
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.crud import referral as ref_crud
from app.database.crud import referral_milestone as milestone_crud
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.crud.user_promo_group import add_user_to_promo_group
from app.database.models import TransactionType, UserReferralMilestoneClaim


logger = structlog.get_logger(__name__)


class ReferralMilestoneService:
    async def reward_milestones(self, db, referrer_id: int, bot=None) -> list[int]:
        """Grant any unclaimed milestones the referrer has reached. Best-effort, idempotent."""
        granted: list[int] = []
        try:
            if not settings.REFERRAL_MILESTONES_ENABLED:
                return granted

            count = await ref_crud.count_paid_referrals(db, referrer_id)
            if count <= 0:
                return granted

            milestones = await milestone_crud.list_active(db)
            reached = [m for m in milestones if m.threshold <= count]
            if not reached:
                return granted

            referrer = await get_user_by_id(db, referrer_id)
            if referrer is None:
                return granted

            claimed = await milestone_crud.get_claimed_milestone_ids(db, referrer_id)

            for m in reached:
                if m.id in claimed:
                    continue
                try:
                    # Reserve the claim first; unique(user,milestone) makes this idempotent
                    # even under concurrent referral payments.
                    db.add(UserReferralMilestoneClaim(user_id=referrer_id, milestone_id=m.id))
                    await db.flush()
                except IntegrityError:
                    await db.rollback()  # another tx already claimed it
                    continue

                if m.reward_type == 'balance':
                    ok = await add_user_balance(
                        db, referrer, m.reward_value,
                        description=f'🎯 Реферальный милстоун: {m.threshold} оплативших',
                        transaction_type=TransactionType.REFERRAL_REWARD, commit=False,
                    )
                    if not ok:
                        await db.rollback()
                        continue
                elif m.reward_type == 'promo_group':
                    await add_user_to_promo_group(db, referrer_id, m.reward_value)
                else:
                    await db.rollback()
                    continue

                await db.commit()
                granted.append(m.id)
                await self._notify(bot, referrer, m)

            return granted
        except Exception as exc:
            logger.error('referral_milestone.reward_failed', referrer_id=referrer_id, err=str(exc))
            try:
                await db.rollback()
            except Exception:
                pass
            return granted

    async def _notify(self, bot, referrer, milestone) -> None:
        if bot is None or not getattr(referrer, 'telegram_id', None):
            return
        title = (milestone.title or {}).get(getattr(referrer, 'language', 'ru')) \
            or (milestone.title or {}).get('ru') or f'{milestone.threshold} рефералов'
        try:
            await bot.send_message(
                referrer.telegram_id,
                f'🎉 <b>Достигнут реферальный милстоун!</b>\n\n{title}',
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.warning('referral_milestone.notify_failed', referrer_id=referrer.id, err=str(exc))


referral_milestone_service = ReferralMilestoneService()
```

- [ ] **Step 4: Run → PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_referral_milestone_service.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/referral_milestone_service.py tests/services/test_referral_milestone_service.py
git commit -m "feat(referral-milestones): ReferralMilestoneService (idempotent grant, balance/promo_group)"
```

---

## Task 4: hook into process_referral_topup

**Files:**
- Modify: `app/services/referral_service.py`

**Context:** `process_referral_topup` (line 316) has two `return True` exits: ~line 408 (commission-before-first-bonus path) and ~line 583 (main/elif paths). Both can create a `ReferralEarning`. Add the milestone hook before BOTH returns (idempotent + recount-based → calling on both paths is safe). `referrer` is in scope at both points.

- [ ] **Step 1: Hook before the early return (~line 408)**

Find the `return True` ending the `if not qualifies_for_first_bonus:` block (~line 408). Immediately before it (matching its indentation — nested inside `if not user.has_made_first_topup:` → `if not qualifies_for_first_bonus:`), add:
```python
                try:
                    from app.services.referral_milestone_service import referral_milestone_service

                    await referral_milestone_service.reward_milestones(db, referrer.id, bot)
                except Exception as exc:
                    logger.error('referral milestone hook failed (pre-first-bonus)', error=exc)
```

- [ ] **Step 2: Hook before the final return (~line 583)**

Replace the final bare `return True` (~line 583, function-body indent, after the `elif commission_amount > 0:` block) with:
```python
        try:
            from app.services.referral_milestone_service import referral_milestone_service

            await referral_milestone_service.reward_milestones(db, referrer.id, bot)
        except Exception as exc:
            logger.error('referral milestone hook failed', error=exc)

        return True
```

- [ ] **Step 3: Verify import + commit**

Run: `.venv/Scripts/python.exe -c "import app.services.referral_service; print('OK')"`
```bash
git add app/services/referral_service.py
git commit -m "feat(referral-milestones): grant hook in process_referral_topup"
```

---

## Task 5: admin CRUD + cabinet progress endpoint

**Files:**
- Create: `app/cabinet/routes/admin_referral_milestones.py`, `app/cabinet/schemas/referral_milestone.py`
- Modify: `app/cabinet/routes/referral.py`, `app/cabinet/routes/__init__.py`

**Context:** Mirror `app/cabinet/routes/admin_partner_promos.py` (admin RBAC CRUD, ValueError→400, schemas file). Cabinet referral router `app/cabinet/routes/referral.py` exists — add a progress endpoint there. CRUD from Task 2 + `count_paid_referrals`. Check the RBAC permission name used by `admin_referral_network.py`/`admin_partners.py` (e.g. `referrals:read`/`referrals:edit` or `partners:*`) — reuse the referral/partner admin permission.

- [ ] **Step 1: Admin CRUD**

Create `app/cabinet/routes/admin_referral_milestones.py` mirroring `admin_partner_promos.py`: list_all/get/create/update/delete/toggle, RBAC-gated, `ValueError`→`HTTPException(400, detail={'code':'invalid','message':str(e)})`. Pydantic models in `app/cabinet/schemas/referral_milestone.py` (threshold int>=1, reward_type Literal['balance','promo_group'], reward_value int>=0, title dict, is_active bool; update = all optional).

- [ ] **Step 2: Cabinet progress endpoint**

In `app/cabinet/routes/referral.py`, add (match its existing imports — `get_current_cabinet_user`, `get_cabinet_db`, `settings`, `HTTPException`, `status`, `User`; add missing):
```python
@router.get('/milestones')
async def referral_milestones(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    if not settings.REFERRAL_MILESTONES_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
    from app.database.crud import referral as ref_crud
    from app.database.crud import referral_milestone as milestone_crud

    count = await ref_crud.count_paid_referrals(db, user.id)
    milestones = await milestone_crud.list_active(db)
    claimed = await milestone_crud.get_claimed_milestone_ids(db, user.id)
    return {
        'count': count,
        'milestones': [
            {'threshold': m.threshold, 'title': m.title, 'reward_type': m.reward_type,
             'reward_value': m.reward_value, 'claimed': m.id in claimed, 'reached': m.threshold <= count}
            for m in milestones
        ],
    }
```
Confirm the referral router prefix so the final path is `/cabinet/referral/milestones` (or whatever its prefix yields — note it for the frontend).

- [ ] **Step 3: Register admin router**

In `app/cabinet/routes/__init__.py`, register the admin milestones router (mirror admin_partner_promos). The `/referral/milestones` endpoint rides the already-registered referral router.

- [ ] **Step 4: Verify + commit**

Run: `.venv/Scripts/python.exe -c "import app.cabinet.routes; print('OK')"`
```bash
git add app/cabinet/routes/admin_referral_milestones.py app/cabinet/schemas/referral_milestone.py app/cabinet/routes/referral.py app/cabinet/routes/__init__.py
git commit -m "feat(referral-milestones): admin CRUD + cabinet progress endpoint"
```

---

## Task 6: bot progress line

**Files:**
- Modify: `app/handlers/referral.py`

**Context:** Bot referral menu renders referral stats. Add a gated milestone progress line. Read `app/handlers/referral.py` to find where the referral message text is built and how `db`/`db_user`/`texts` are in scope.

- [ ] **Step 1: Read the referral menu render**

Find where the referral menu message text is composed (referral stats / earnings). Note variable names (`text`/`message`, `db`, `db_user`, `texts`).

- [ ] **Step 2: Add gated progress line**

Where the message is built (with `db` + `db_user` in scope), add:
```python
    if settings.REFERRAL_MILESTONES_ENABLED:
        from app.database.crud import referral as ref_crud
        from app.database.crud import referral_milestone as milestone_crud

        paid_count = await ref_crud.count_paid_referrals(db, db_user.id)
        active_ms = await milestone_crud.list_active(db)
        next_ms = next((m for m in active_ms if m.threshold > paid_count), None)
        if next_ms:
            text += '\n\n' + texts.t(
                'REFERRAL_MILESTONE_PROGRESS',
                '🎯 Оплативших рефералов: {count} · до награды ({next}): {left}',
            ).format(count=paid_count, next=next_ms.threshold, left=next_ms.threshold - paid_count)
        elif active_ms:
            text += '\n\n' + texts.t('REFERRAL_MILESTONE_ALL_DONE', '🎯 Все реферальные милстоуны достигнуты!')
```
Adapt the message-variable name + ensure `settings` imported. Place where `db`/`db_user` are available.

- [ ] **Step 3: Verify + commit**

Run: `.venv/Scripts/python.exe -c "import app.handlers.referral; print('OK')"`
```bash
git add app/handlers/referral.py
git commit -m "feat(referral-milestones): bot referral-menu progress line"
```

---

## Task 7: cabinet React progress (NESTED repo) + finalize

**Files (NESTED cabinet repo):** referral api module (+getMilestones), `src/pages/Referral.tsx`. **Main repo:** `.env.example`.

**Context:** Cabinet = NESTED git repo. Endpoint path from Task 5 (likely `/cabinet/referral/milestones`; apiClient base `/api` → call `/cabinet/referral/milestones`). Mirror the speedtest/showcase frontend pattern + existing Referral.tsx fetch.

- [ ] **Step 1: Frontend (nested repo)**

In `bedolaga-cabinet/`: `git checkout main && git checkout -b feat/referral-milestones`.
- Add `getMilestones()` to the referral api module (find it: `src/api/referral.ts`) → `apiClient.get('/cabinet/referral/milestones')`, typed `{count, milestones: [{threshold, title, reward_type, reward_value, claimed, reached}]}`.
- In `src/pages/Referral.tsx`, add a milestones block: progress (count, next threshold), list with claimed/reached/locked state. Reuse existing card/progress components. Hide block on 404 (feature disabled). i18n RU/EN. title resolve: `title[i18n.language] || title.ru || Object.values(title)[0]`.
- Build: `npx tsc --noEmit` (no new errors) + `npm run build`. Commit nested repo:
```bash
git add src/api/ src/pages/Referral.tsx src/locales/
git commit -m "feat(referral-milestones): cabinet referral milestone progress block"
```

- [ ] **Step 2: .env.example (main repo)**

Add near other feature flags:
```
# Referral milestones: cumulative rewards for N paying referrals (on top of flat+commission). Default off.
REFERRAL_MILESTONES_ENABLED=false
```

- [ ] **Step 3: Final backend verify (main repo)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_referral_milestone_service.py -v` → PASS.
Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q` → no NEW failures vs baseline (~29 pre-existing).
Run: `.venv/Scripts/python.exe -c "import app.services.referral_service; import app.cabinet.routes; import app.handlers.referral; print('OK')"`.

- [ ] **Step 4: Commit (main repo)**

```bash
git add .env.example
git commit -m "docs(referral-milestones): env flag in .env.example"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] Migration 0100 single head, down_revision 0099, reversible. unique(threshold) + unique(user,milestone).
- [ ] count_paid_referrals = DISTINCT referral_id with amount>0 (anti-fraud, not registrations).
- [ ] Service: gated; grants only reached + unclaimed; claim-insert(flush) before reward (idempotent via unique + IntegrityError catch); balance via commit=False atomic with claim commit.
- [ ] Hook in process_referral_topup at BOTH return paths, best-effort try/except (never breaks referral flow).
- [ ] promo_group reward → add_user_to_promo_group; balance → add_user_balance.
- [ ] Admin RBAC-gated; cabinet progress endpoint gated by flag.
- [ ] Frontend in NESTED cabinet repo.
- [ ] All gated by REFERRAL_MILESTONES_ENABLED (default OFF).

## Out of plan scope (follow-ups)

- React admin-UI for milestones (v1 = backend CRUD).
- Reward types: subscription days / devices (v1 = balance/promo_group).
- Backfill for pre-existing referrers (first hook after enable grants reached milestones naturally).
- Revoking promo_group on milestone deactivation (rewards not clawed back).
