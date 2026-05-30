# Триал-за-инвайт Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Когда приглашённый активирует свой триал, продлить триал инвайтера (если он сам ещё на триале) на N дней, под суммарным капом, с защитой от гонок и абуза.

**Architecture:** `TrialInviteService.reward_inviter_on_trial_activation(db, invitee, bot)` вызывается из двух точек активации триала (бот + кабинет) ПОСЛЕ успешного provision на панели. Реюз реферал-атрибуции (`User.referred_by_id`). Продление триала инвайтера под `SELECT FOR UPDATE`-локом. Панель досинкивается через `create_remnawave_user`. За env-флагом (дефолт OFF).

**Tech Stack:** Python 3.12, aiogram 3.x, SQLAlchemy async, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-05-30-trial-for-invite-design.md`

**Run tests:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `migrations/alembic/versions/0098_add_trial_invite_fields.py` + `app/database/models.py` — миграция + 2 счётчика User (Task 1).
- `app/config.py` — 3 env-настройки (Task 1).
- `app/services/trial_invite_service.py` — сервис + singleton (Task 2).
- `app/handlers/subscription/purchase.py` + `app/cabinet/routes/subscription_modules/purchase.py` — 2 хука (Task 3).
- `tests/services/test_trial_invite_service.py` — тесты (Task 2).

---

## Task 1: миграция + поля User + config

**Files:**
- Create: `migrations/alembic/versions/0098_add_trial_invite_fields.py`
- Modify: `app/database/models.py` (class User), `app/config.py`

- [ ] **Step 1: Add User counter fields**

In `app/database/models.py`, `class User`, near the other referral/trial fields, add:

```python
    trial_invite_bonus_days_used = Column(Integer, default=0, nullable=False, server_default='0')
    trial_invite_rewarded_count = Column(Integer, default=0, nullable=False, server_default='0')
```

`Integer` is already imported.

- [ ] **Step 2: Create migration**

Create `migrations/alembic/versions/0098_add_trial_invite_fields.py`:

```python
"""add trial-invite counters to users

Revision ID: 0098
Revises: 0097
Create Date: 2026-05-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0098'
down_revision: Union[str, None] = '0097'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('trial_invite_bonus_days_used', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('trial_invite_rewarded_count', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('users', 'trial_invite_rewarded_count')
    op.drop_column('users', 'trial_invite_bonus_days_used')
```

Confirm `0097` is the current single head (it is — `0097_add_freeze_fields.py`). If not, set down_revision to the real head.

- [ ] **Step 3: Add config**

In `app/config.py`, near `TRIAL_DURATION_DAYS` (~line 148), add:

```python
    TRIAL_INVITE_ENABLED: bool = False
    TRIAL_INVITE_EXTEND_DAYS: int = 3
    TRIAL_INVITE_MAX_EXTENSION_DAYS: int = 14
```

And add two getter methods near the other trial getters (with clamps):

```python
    def get_trial_invite_extend_days(self) -> int:
        try:
            return max(0, min(365, int(self.TRIAL_INVITE_EXTEND_DAYS)))
        except (TypeError, ValueError):
            return 3

    def get_trial_invite_max_extension_days(self) -> int:
        try:
            return max(0, min(365, int(self.TRIAL_INVITE_MAX_EXTENSION_DAYS)))
        except (TypeError, ValueError):
            return 14
```

- [ ] **Step 4: Verify**

Run: `.venv/Scripts/python.exe -c "import app.database.models; import app.config; print(app.config.settings.TRIAL_INVITE_ENABLED)"` → prints `False`.
Run: `.venv/Scripts/python.exe -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print('heads:', s.get_heads())"` → single head `('0098',)`.

- [ ] **Step 5: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/0098_add_trial_invite_fields.py app/config.py
git commit -m "feat(trial-invite): User counters (migration 0098) + config flags"
```

---

## Task 2: TrialInviteService

**Files:**
- Create: `app/services/trial_invite_service.py`
- Test: `tests/services/test_trial_invite_service.py`

**Context:** `User` has `id`, `referred_by_id`, `telegram_id`, `language`, and the new `trial_invite_bonus_days_used`/`trial_invite_rewarded_count`. `Subscription` has `id`, `user_id`, `is_trial`, `status`, `end_date`, `remnawave_uuid`. `SubscriptionStatus.ACTIVE.value == 'active'`. `get_user_by_id(db, id)` in `app/database/crud/user.py`. `SubscriptionService().create_remnawave_user(db, subscription)` re-syncs a subscription (incl. end_date) to the RemnaWave panel. `remnawave_retry_queue.enqueue(subscription_id, user_id, action='update')`. Mirror the structured-logging + singleton style of `freeze_service.py`.

- [ ] **Step 1: Write failing tests**

Create `tests/services/test_trial_invite_service.py`:

```python
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.trial_invite_service as tis
from app.services.trial_invite_service import TrialInviteService


def _user(**kw):
    base = dict(id=10, referred_by_id=None, telegram_id=555, language='ru',
                trial_invite_bonus_days_used=0, trial_invite_rewarded_count=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _trial_sub(**kw):
    now = datetime.now(UTC)
    base = dict(id=99, user_id=1, is_trial=True, status='active',
                end_date=now + timedelta(days=2), remnawave_uuid='uuid-inv')
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def service():
    svc = TrialInviteService()
    svc._subscription_service = MagicMock()
    svc._subscription_service.create_remnawave_user = AsyncMock(return_value=None)
    svc._notify = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(tis.settings, 'TRIAL_INVITE_ENABLED', True, raising=False)
    monkeypatch.setattr(tis.settings.__class__, 'get_trial_invite_extend_days', lambda self: 3, raising=False)
    monkeypatch.setattr(tis.settings.__class__, 'get_trial_invite_max_extension_days', lambda self: 14, raising=False)
    yield


def _db(referrer=None, locked_sub=None):
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    # get_user_by_id returns the referrer
    # _select_active_trial_for_update returns the locked sub via db.execute
    result = MagicMock()
    result.scalar_one_or_none.return_value = locked_sub
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_reward_happy(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1, trial_invite_bonus_days_used=0)
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date

    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)

    assert inv_sub.end_date == old_end + timedelta(days=3)
    assert referrer.trial_invite_bonus_days_used == 3
    assert referrer.trial_invite_rewarded_count == 1
    service._subscription_service.create_remnawave_user.assert_awaited_once()
    service._notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_referrer_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=None)
    db = _db()
    get_user = AsyncMock()
    monkeypatch.setattr(tis, 'get_user_by_id', get_user)
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    get_user.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_invite_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=10)
    db = _db()
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=invitee))
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_referrer_not_on_trial_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1)
    db = _db(locked_sub=None)  # no active trial sub found
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    service._subscription_service.create_remnawave_user.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_exhausted_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1, trial_invite_bonus_days_used=14)
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    assert inv_sub.end_date == old_end  # unchanged
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_partial_grant(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1, trial_invite_bonus_days_used=12)  # remaining=2, extend=3 -> grant 2
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    assert inv_sub.end_date == old_end + timedelta(days=2)
    assert referrer.trial_invite_bonus_days_used == 14


@pytest.mark.asyncio
async def test_disabled_flag_noop(service, monkeypatch):
    monkeypatch.setattr(tis.settings, 'TRIAL_INVITE_ENABLED', False, raising=False)
    invitee = _user(id=10, referred_by_id=1)
    get_user = AsyncMock()
    monkeypatch.setattr(tis, 'get_user_by_id', get_user)
    db = _db()
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    get_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_failure_keeps_time_enqueues(service, monkeypatch):
    service._subscription_service.create_remnawave_user = AsyncMock(side_effect=RuntimeError('panel down'))
    enqueue = MagicMock()
    monkeypatch.setattr(tis.remnawave_retry_queue, 'enqueue', enqueue)
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1)
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    assert inv_sub.end_date == old_end + timedelta(days=3)  # time kept
    db.commit.assert_awaited()
    enqueue.assert_called_once()
```

- [ ] **Step 2: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_trial_invite_service.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `app/services/trial_invite_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Subscription, SubscriptionStatus
from app.services.remnawave_retry_queue import remnawave_retry_queue
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)


class TrialInviteService:
    def __init__(self) -> None:
        self._subscription_service = SubscriptionService()

    async def reward_inviter_on_trial_activation(self, db, invitee, bot=None) -> None:
        """Extend the inviter's own trial when their invitee activates a trial.

        Best-effort: never raises into the invitee's activation flow.
        """
        try:
            if not settings.TRIAL_INVITE_ENABLED:
                return

            referrer_id = getattr(invitee, 'referred_by_id', None)
            if not referrer_id or referrer_id == invitee.id:
                return

            referrer = await get_user_by_id(db, referrer_id)
            if referrer is None:
                return

            now = datetime.now(UTC)

            # Lock the inviter's active trial subscription (serialize concurrent invitees)
            locked = await db.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == referrer.id,
                    Subscription.is_trial == True,  # noqa: E712
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > now,
                )
                .order_by(Subscription.end_date.desc())
                .with_for_update()
            )
            inviter_sub = locked.scalar_one_or_none()
            if inviter_sub is None:
                return  # inviter not on an active trial — normal referral mechanics apply

            extend = settings.get_trial_invite_extend_days()
            max_ext = settings.get_trial_invite_max_extension_days()
            used = referrer.trial_invite_bonus_days_used or 0
            remaining = max(0, max_ext - used)
            grant = min(extend, remaining)
            if grant <= 0:
                return  # cap exhausted

            inviter_sub.end_date = inviter_sub.end_date + timedelta(days=grant)
            referrer.trial_invite_bonus_days_used = used + grant
            referrer.trial_invite_rewarded_count = (referrer.trial_invite_rewarded_count or 0) + 1

            await db.commit()

            # Sync the extended end_date to the panel (best-effort).
            try:
                await self._subscription_service.create_remnawave_user(db, inviter_sub)
            except Exception as exc:
                remnawave_retry_queue.enqueue(
                    subscription_id=inviter_sub.id, user_id=referrer.id, action='update',
                )
                logger.warning('trial_invite.panel_sync_failed_enqueued', subscription_id=inviter_sub.id, err=str(exc))

            await self._notify(bot, referrer, grant)
            logger.info('trial_invite.granted', referrer_id=referrer.id, invitee_id=invitee.id, days=grant)

        except Exception as exc:
            logger.error('trial_invite.reward_failed', invitee_id=getattr(invitee, 'id', None), err=str(exc))
            try:
                await db.rollback()
            except Exception:
                pass

    async def _notify(self, bot, referrer, days: int) -> None:
        if bot is None or not getattr(referrer, 'telegram_id', None):
            return
        text = f'🎁 Ваш друг активировал триал — вам +{days} дн. к триалу!'
        try:
            await bot.send_message(referrer.telegram_id, text, parse_mode='HTML')
        except Exception as exc:
            logger.warning('trial_invite.notify_failed', referrer_id=referrer.id, err=str(exc))


trial_invite_service = TrialInviteService()
```

- [ ] **Step 4: Run → PASS (8 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_trial_invite_service.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/trial_invite_service.py tests/services/test_trial_invite_service.py
git commit -m "feat(trial-invite): TrialInviteService with cap + row-lock + panel sync"
```

---

## Task 3: hook into the two trial-activation sites

**Files:**
- Modify: `app/handlers/subscription/purchase.py` (~line 1062, after `create_remnawave_user` succeeds)
- Modify: `app/cabinet/routes/subscription_modules/purchase.py` (~line 1385, after `create_remnawave_user`)

**Context:** Both sites create the invitee's trial then provision on the panel via `subscription_service.create_remnawave_user(db, subscription)`. Hook the reward AFTER successful provision so we only reward on a real activation. The hook must be best-effort (its own try/except) and must not break the invitee's flow. `db_user` (bot) / `user` (cabinet) is the invitee.

- [ ] **Step 1: Hook the bot site**

In `app/handlers/subscription/purchase.py`, locate (~line 1057-1062):
```python
        subscription_service = SubscriptionService()
        try:
            remnawave_user = await subscription_service.create_remnawave_user(
                db,
                subscription,
            )
```
Find where this try-block succeeds (after the success path, before the handler returns the success message to the user — i.e. after provision is confirmed). Add:
```python
        try:
            from app.services.trial_invite_service import trial_invite_service

            await trial_invite_service.reward_inviter_on_trial_activation(db, db_user, callback.bot)
        except Exception as exc:
            logger.error('trial_invite hook failed (bot)', error=exc)
```
Place it after the trial is fully activated+provisioned (after the `create_remnawave_user` success, outside its except). Use the real bot reference available in the handler (`callback.bot`).

- [ ] **Step 2: Hook the cabinet site**

In `app/cabinet/routes/subscription_modules/purchase.py`, after the trial RemnaWave provision block (~line 1384-1385, after `await subscription_service.create_remnawave_user(db, subscription)` / `await db.refresh(subscription)`), add:
```python
    try:
        from app.services.trial_invite_service import trial_invite_service

        bot = None
        if settings.BOT_TOKEN:
            from aiogram import Bot

            bot = Bot(token=settings.BOT_TOKEN)
        await trial_invite_service.reward_inviter_on_trial_activation(db, user, bot)
    except Exception as e:
        logger.error('trial_invite hook failed (cabinet)', error=e)
```
(If the cabinet module already constructs a `Bot` for admin notifications nearby — it does, ~line 1402-1403 — reuse that pattern; don't double-construct if a bot is already available in scope. Match the file's real approach to obtaining a bot.)

- [ ] **Step 3: Verify imports**

Run: `.venv/Scripts/python.exe -c "import app.handlers.subscription.purchase; import app.cabinet.routes.subscription_modules.purchase; print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add app/handlers/subscription/purchase.py app/cabinet/routes/subscription_modules/purchase.py
git commit -m "feat(trial-invite): reward hook at bot + cabinet trial activation"
```

---

## Task 4: full verification

- [ ] **Step 1: Run trial-invite tests + regression**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_trial_invite_service.py -v` → 8 PASS.
Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q` → no NEW failures vs baseline (~29 pre-existing acceptable; confirm none in trial_invite files).

- [ ] **Step 2: .env.example**

Add near trial settings:
```
# Trial-for-invite: extend an inviter's own trial when their invitee activates a trial.
TRIAL_INVITE_ENABLED=false
TRIAL_INVITE_EXTEND_DAYS=3
TRIAL_INVITE_MAX_EXTENSION_DAYS=14
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(trial-invite): document env flags in .env.example"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] Migration 0098 single head, down_revision 0097, reversible.
- [ ] Model fields match migration column names exactly.
- [ ] Reward gated by TRIAL_INVITE_ENABLED; skips no-referrer / self / not-on-trial / cap-exhausted.
- [ ] Inviter trial sub loaded FOR UPDATE before mutation (concurrent-invitee race safe).
- [ ] grant = min(extend, max - used); counters incremented; commit once.
- [ ] Panel sync best-effort; failure keeps DB time + enqueues retry.
- [ ] Hook is best-effort at BOTH sites (own try/except), never breaks invitee activation.
- [ ] Hook placed AFTER successful panel provision (real activation, not just DB row).

## Out of plan scope (follow-ups)

- Runtime admin-JSON panel for the 3 settings (env-only in v1).
- Localizing the inviter notification across locales (inline RU default).
- Rewarding non-trial inviters / milestone stacking (separate feature #8).
- One-time "cap reached" notification to the inviter.
