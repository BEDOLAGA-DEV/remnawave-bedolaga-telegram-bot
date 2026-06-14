# Auto-activate Achievements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unlock achievements (credit rewards + notify) automatically in the monitoring cycle for active/recent users, instead of only when the user opens the achievements menu.

**Architecture:** Reuse `check_and_unlock_all` (the existing per-user evaluate+unlock+reward+notify function). Add a candidate-selection crud helper and a `_check_achievements` sweep step in the monitoring cycle that calls `check_and_unlock_all` per active/recent user in a fresh per-user session. The menu-open path stays unchanged.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, aiogram 3, pytest (`.venv/Scripts/python.exe -m pytest`).

---

## File Structure

- `app/config.py` — two settings (gate + active-days window).
- `app/database/crud/achievement.py` — `get_achievement_sweep_user_ids(db, active_days)` helper.
- `app/services/monitoring_service.py` — `_check_achievements(self, db)` + call in `_monitoring_cycle`.
- `tests/services/test_achievements_sweep.py` — tests for the helper (union/dedup) and the sweep gate/dispatch.

Run tests: `.venv/Scripts/python.exe -m pytest <path> -q`. Commit after each task.

---

## Task 1: Settings

**Files:**
- Modify: `app/config.py:1193`

- [ ] **Step 1: Add settings** right after `ACHIEVEMENTS_ENABLED: bool = True`:

```python
    ACHIEVEMENTS_ENABLED: bool = True
    ACHIEVEMENTS_AUTO_CHECK_ENABLED: bool = True
    ACHIEVEMENTS_SWEEP_ACTIVE_DAYS: int = 7
```

- [ ] **Step 2: Smoke-import**

Run: `.venv/Scripts/python.exe -c "from app.config import settings; print(settings.ACHIEVEMENTS_AUTO_CHECK_ENABLED, settings.ACHIEVEMENTS_SWEEP_ACTIVE_DAYS)"`
Expected: `True 7`

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat(achievements): add auto-check sweep settings"
```

---

## Task 2: Candidate-selection helper (TDD)

`get_achievement_sweep_user_ids` returns the union of (active/trial-subscription users) and
(active users updated within `active_days`), as a deduped list of user ids. It performs
exactly two `db.execute(...)` calls; the test drives it with a fake session.

**Files:**
- Create: `tests/services/test_achievements_sweep.py`
- Modify: `app/database/crud/achievement.py` (add helper; imports `User`, `Subscription`, `SubscriptionStatus`, `select`, `datetime/UTC/timedelta` already present at top)

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_achievements_sweep.py
from types import SimpleNamespace

import pytest

from app.database.crud.achievement import get_achievement_sweep_user_ids


class _FakeResult:
    def __init__(self, ids):
        self._ids = ids

    def scalars(self):
        return self

    def all(self):
        return self._ids


class _FakeDB:
    """Returns canned id lists for sequential execute() calls."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, *args, **kwargs):
        res = _FakeResult(self._results[self.calls])
        self.calls += 1
        return res


@pytest.mark.asyncio
async def test_sweep_user_ids_unions_and_dedupes():
    db = _FakeDB([[1, 2, 3], [3, 4]])  # active-sub ids, then recent-active ids
    ids = await get_achievement_sweep_user_ids(db, active_days=7)
    assert sorted(ids) == [1, 2, 3, 4]
    assert db.calls == 2  # one query per source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_achievements_sweep.py -q`
Expected: FAIL — `ImportError: cannot import name 'get_achievement_sweep_user_ids'`.

- [ ] **Step 3: Implement the helper** in `app/database/crud/achievement.py` (append near the other module-level functions, e.g. after `get_user_achievements`):

```python
async def get_achievement_sweep_user_ids(db: AsyncSession, active_days: int) -> list[int]:
    """User ids to evaluate in the background achievement sweep.

    Union of:
      - users with an active/trial subscription
      - active users updated within ``active_days`` days
    Both restricted to telegram_id IS NOT NULL. Returns a deduped list.

    Uses id-only selects (no SELECT DISTINCT on User rows — the users table has a
    json column with no equality operator, which breaks DISTINCT on whole rows).
    """
    cutoff = datetime.now(UTC) - timedelta(days=active_days)

    sub_ids_result = await db.execute(
        select(Subscription.user_id)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
            User.telegram_id.isnot(None),
        )
    )
    recent_ids_result = await db.execute(
        select(User.id).where(
            User.status == 'active',
            User.telegram_id.isnot(None),
            User.updated_at >= cutoff,
        )
    )

    ids: set[int] = set(sub_ids_result.scalars().all())
    ids.update(recent_ids_result.scalars().all())
    return list(ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_achievements_sweep.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/database/crud/achievement.py tests/services/test_achievements_sweep.py
git commit -m "feat(achievements): candidate-selection helper for the sweep"
```

---

## Task 3: Background sweep `_check_achievements` (TDD)

**Files:**
- Modify: `app/services/monitoring_service.py` — add `_check_achievements`; call it in `_monitoring_cycle` after `await self._sync_with_remnawave(db)` (~line 298).
- Modify: `tests/services/test_achievements_sweep.py` — add gate + dispatch tests.

- [ ] **Step 1: Write the failing tests** (append):

```python
from unittest.mock import AsyncMock

import app.database.crud.achievement as ach_crud
from app.services.monitoring_service import MonitoringService


@pytest.mark.asyncio
async def test_sweep_skipped_when_disabled(monkeypatch):
    import app.services.monitoring_service as ms

    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_ENABLED', True, raising=False)
    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_AUTO_CHECK_ENABLED', False, raising=False)
    called = AsyncMock()
    monkeypatch.setattr(ach_crud, 'check_and_unlock_all', called)

    svc = MonitoringService(bot=AsyncMock())
    await svc._check_achievements(db=None)

    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_dispatches_per_candidate(monkeypatch):
    import app.services.monitoring_service as ms

    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_ENABLED', True, raising=False)
    monkeypatch.setattr(ms.settings, 'ACHIEVEMENTS_AUTO_CHECK_ENABLED', True, raising=False)

    async def fake_ids(db, active_days):
        return [101, 202, 303]

    monkeypatch.setattr(ach_crud, 'get_achievement_sweep_user_ids', fake_ids)
    unlock = AsyncMock(return_value=[])
    monkeypatch.setattr(ach_crud, 'check_and_unlock_all', unlock)

    # Avoid real DB sessions — make AsyncSessionLocal yield a dummy async-ctx session.
    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr(ms, 'AsyncSessionLocal', lambda: _DummySession())

    svc = MonitoringService(bot=AsyncMock())
    await svc._check_achievements(db=None)

    assert unlock.await_count == 3
    assert {c.args[1] for c in unlock.await_args_list} == {101, 202, 303}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_achievements_sweep.py -q`
Expected: FAIL — `_check_achievements` missing.

- [ ] **Step 3: Implement `_check_achievements`** in `MonitoringService` (place near the other `_check_*` methods, e.g. after `_sync_with_remnawave`):

```python
    async def _check_achievements(self, db: AsyncSession):
        """Background sweep: unlock earned achievements for active/recent users.

        Reuses check_and_unlock_all (evaluate + unlock + reward + notify). Each user
        is processed in a fresh session so the user-row FOR UPDATE lock stays short
        and one user's failure never aborts the sweep.
        """
        if not settings.ACHIEVEMENTS_ENABLED or not getattr(settings, 'ACHIEVEMENTS_AUTO_CHECK_ENABLED', True):
            return
        if not self.bot:
            return

        from app.database.crud.achievement import check_and_unlock_all, get_achievement_sweep_user_ids

        active_days = int(getattr(settings, 'ACHIEVEMENTS_SWEEP_ACTIVE_DAYS', 7))
        try:
            user_ids = await get_achievement_sweep_user_ids(db, active_days)
        except Exception as e:
            logger.error('Achievements sweep: failed to load candidates', error=e)
            return

        if not user_ids:
            return

        batch_size = 25
        users_with_unlocks = 0
        for i in range(0, len(user_ids), batch_size):
            for uid in user_ids[i : i + batch_size]:
                try:
                    async with AsyncSessionLocal() as session:
                        unlocked = await check_and_unlock_all(session, uid, bot=self.bot)
                        await session.commit()
                        if unlocked:
                            users_with_unlocks += 1
                except Exception as e:
                    logger.warning('Achievements sweep: user failed', user_id=uid, error=e)
            await asyncio.sleep(0.2)

        logger.info(
            'Achievements sweep done',
            candidates=len(user_ids),
            users_with_unlocks=users_with_unlocks,
        )
```

- [ ] **Step 4: Wire into the monitoring cycle.** In `_monitoring_cycle`, after `await self._sync_with_remnawave(db)` add:

```python
                await self._sync_with_remnawave(db)
                await self._check_achievements(db)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_achievements_sweep.py -q`
Expected: PASS (all).

- [ ] **Step 6: Smoke-import**

Run: `.venv/Scripts/python.exe -c "import app.services.monitoring_service"`
Expected: no error.

- [ ] **Step 7: Commit**

```bash
git add app/services/monitoring_service.py tests/services/test_achievements_sweep.py
git commit -m "feat(achievements): background sweep in monitoring cycle"
```

---

## Task 4: Full verification

- [ ] **Step 1: Run the new tests + the achievement regression suite**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_achievements_sweep.py tests/regression -q -k "achievement or sweep"`
Expected: all PASS.

- [ ] **Step 2: Smoke-import**

Run: `.venv/Scripts/python.exe -c "import app.config, app.database.crud.achievement, app.services.monitoring_service"`
Expected: no error.

- [ ] **Step 3: Final review** — confirm: two settings present; helper unions active-sub + recent-active and dedupes; `_check_achievements` gated, per-user fresh session + commit, per-user try/except, called in `_monitoring_cycle`; menu-open path untouched.

---

## Self-review notes

- **Spec coverage:** candidate selection (Task 2), background sweep + gate + per-user session (Task 3), settings (Task 1), menu-open unchanged (not touched), tests for helper + gate + dispatch (Tasks 2/3). ✅
- **Names consistent:** `get_achievement_sweep_user_ids`, `_check_achievements`, `ACHIEVEMENTS_AUTO_CHECK_ENABLED`, `ACHIEVEMENTS_SWEEP_ACTIVE_DAYS` used identically throughout. ✅
- **No event hooks** — out of scope; sweep covers all condition types incl. time-based. ✅
- **Perf:** `check_and_unlock_all` skips already-unlocked templates before any stat query, so completed users are cheap; cost ∝ users with pending achievements.
