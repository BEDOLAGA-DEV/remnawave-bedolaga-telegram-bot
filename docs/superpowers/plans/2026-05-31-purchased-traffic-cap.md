# Кап докупленного трафика Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ограничить суммарный активный докупленный трафик на подписку конфигурируемым капом; при превышении — отклонять покупку ДО списания денег. Дефолт 0 (без лимита). A (показ остатка) уже есть — не трогаем.

**Architecture:** Helper `can_add_purchased_traffic` считает активный докупленный по `TrafficPurchase` (неистёкшие) и сравнивает с капом. Вызывается в юзер-точках покупки (бот + кабинет) ПЕРЕД charge. webapi (API-token) и admin-bulk не капятся. Миграции нет.

**Tech Stack:** Python 3.12, FastAPI, aiogram, SQLAlchemy async, pytest.

**Spec:** `docs/superpowers/specs/2026-05-31-purchased-traffic-cap-design.md`

**Run tests:** `.venv/Scripts/python.exe -m pytest <path> -v`

---

## File Structure

- `app/config.py` — `MAX_PURCHASED_TRAFFIC_GB` + getter (Task 1).
- `app/database/crud/subscription.py` — `get_active_purchased_traffic_gb` + `can_add_purchased_traffic` (Task 1).
- `app/handlers/subscription/traffic.py` — bot pre-check before charge (Task 2).
- `app/cabinet/routes/subscription_modules/traffic.py` — cabinet pre-check before charge (Task 3).
- `tests/services/test_purchased_traffic_cap.py` — Task 1 tests.

---

## Task 1: config + cap helper

**Files:**
- Modify: `app/config.py`, `app/database/crud/subscription.py`
- Create: `tests/services/test_purchased_traffic_cap.py`

**Context:** `TrafficPurchase` model has `subscription_id`, `traffic_gb`, `expires_at`. `crud/subscription.py` already uses `select`/`delete`; verify whether `func` is imported (`from sqlalchemy import func`) — add if missing. `settings`, `datetime`, `UTC` are imported in that module.

- [ ] **Step 1: Add config**

In `app/config.py` `class Settings`, near other traffic settings (e.g. near `RESET_TRAFFIC_ON_PAYMENT`), add:
```python
    MAX_PURCHASED_TRAFFIC_GB: int = 0  # 0 = no cap on accumulated purchased traffic
```
Add getter near other getters:
```python
    def get_max_purchased_traffic_gb(self) -> int:
        try:
            return max(0, int(self.MAX_PURCHASED_TRAFFIC_GB))
        except (TypeError, ValueError):
            return 0
```

- [ ] **Step 2: Write failing tests**

Create `tests/services/test_purchased_traffic_cap.py`:
```python
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.database.crud.subscription as crud


def _db_with_active(active_gb: int):
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = active_gb
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_cap_zero_always_allows(monkeypatch):
    monkeypatch.setattr(crud.settings, 'get_max_purchased_traffic_gb', lambda: 0, raising=False)
    db = _db_with_active(999)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 500)
    assert allowed is True
    assert remaining == -1


@pytest.mark.asyncio
async def test_cap_allows_within(monkeypatch):
    monkeypatch.setattr(crud.settings, 'get_max_purchased_traffic_gb', lambda: 100, raising=False)
    db = _db_with_active(0)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 50)
    assert allowed is True
    assert remaining == 100


@pytest.mark.asyncio
async def test_cap_rejects_over(monkeypatch):
    monkeypatch.setattr(crud.settings, 'get_max_purchased_traffic_gb', lambda: 100, raising=False)
    db = _db_with_active(80)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 50)
    assert allowed is False
    assert remaining == 20


@pytest.mark.asyncio
async def test_cap_exact_boundary_allows(monkeypatch):
    monkeypatch.setattr(crud.settings, 'get_max_purchased_traffic_gb', lambda: 100, raising=False)
    db = _db_with_active(80)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 20)
    assert allowed is True
    assert remaining == 20


@pytest.mark.asyncio
async def test_cap_full_rejects(monkeypatch):
    monkeypatch.setattr(crud.settings, 'get_max_purchased_traffic_gb', lambda: 100, raising=False)
    db = _db_with_active(100)
    allowed, remaining = await crud.can_add_purchased_traffic(db, 1, 1)
    assert allowed is False
    assert remaining == 0


@pytest.mark.asyncio
async def test_get_active_sums(monkeypatch):
    db = _db_with_active(42)
    total = await crud.get_active_purchased_traffic_gb(db, 1)
    assert total == 42
```

- [ ] **Step 3: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_purchased_traffic_cap.py -v` → AttributeError (functions missing) / config method missing.

- [ ] **Step 4: Implement helpers**

In `app/database/crud/subscription.py`, near `add_subscription_traffic` (~line 835), add. FIRST ensure `func` is importable: check the module's `from sqlalchemy import ...` line; if `func` absent, add it.

```python
async def get_active_purchased_traffic_gb(db: AsyncSession, subscription_id: int) -> int:
    """Sum of non-expired purchased traffic (the source of truth for the cap)."""
    from app.database.models import TrafficPurchase

    now = datetime.now(UTC)
    result = await db.execute(
        select(func.coalesce(func.sum(TrafficPurchase.traffic_gb), 0))
        .where(TrafficPurchase.subscription_id == subscription_id)
        .where(TrafficPurchase.expires_at > now)
    )
    return int(result.scalar() or 0)


async def can_add_purchased_traffic(db: AsyncSession, subscription_id: int, gb: int) -> tuple[bool, int]:
    """Whether `gb` more purchased traffic may be added under the configured cap.

    Returns (allowed, remaining_headroom_gb). remaining = -1 when uncapped.
    Cap is enforced at user-facing purchase flows (bot/cabinet), NOT inside
    add_subscription_traffic (also used by trusted admin/API callers).
    """
    cap = settings.get_max_purchased_traffic_gb()
    if cap <= 0:
        return True, -1
    active = await get_active_purchased_traffic_gb(db, subscription_id)
    remaining = max(0, cap - active)
    return (gb <= remaining), remaining
```

- [ ] **Step 5: Run → PASS (6 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_purchased_traffic_cap.py -v`
Also: `.venv/Scripts/python.exe -c "import app.config; import app.database.crud.subscription; print(app.config.settings.get_max_purchased_traffic_gb())"` → `0`.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/database/crud/subscription.py tests/services/test_purchased_traffic_cap.py
git commit -m "feat(traffic-cap): config + can_add_purchased_traffic helper"
```

---

## Task 2: bot pre-check before charge

**Files:**
- Modify: `app/handlers/subscription/traffic.py`

**Context:** `add_traffic` handler charges via `subtract_user_balance` (~line 639), then `add_subscription_traffic` (~line 662). `subscription`, `db_user`, `traffic_gb`, `callback`, `texts`, `db` are in scope. Cap-check runs BEFORE `subtract_user_balance`, only for `traffic_gb > 0` (gb==0 = switch to unlimited → wipes purchases, not a cap concern). `settings` already imported (`from app.config import PERIOD_PRICES, settings`).

- [ ] **Step 1: Read the charge region**

Read `app/handlers/subscription/traffic.py` ~lines 600-665 — find the point after `traffic_gb`/`price` resolved and before `subtract_user_balance` (~639). Confirm variable names.

- [ ] **Step 2: Insert the cap-check**

Immediately BEFORE the `try:` / `subtract_user_balance` block (after `traffic_gb` and `price` resolved), add:
```python
        if traffic_gb > 0:
            from app.database.crud.subscription import can_add_purchased_traffic

            allowed, remaining = await can_add_purchased_traffic(db, subscription.id, traffic_gb)
            if not allowed:
                cap = settings.get_max_purchased_traffic_gb()
                await callback.answer(
                    texts.t(
                        'TRAFFIC_CAP_REACHED',
                        '⚠️ Достигнут лимит докупленного трафика ({cap} ГБ). Доступно ещё: {remaining} ГБ.',
                    ).format(cap=cap, remaining=remaining),
                    show_alert=True,
                )
                return
```
Placement guarantees NO charge when rejected.

- [ ] **Step 3: Verify import**

Run: `.venv/Scripts/python.exe -c "import app.handlers.subscription.traffic; print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add app/handlers/subscription/traffic.py
git commit -m "feat(traffic-cap): bot purchase cap pre-check before charge"
```

---

## Task 3: cabinet pre-check before charge + finalize

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/traffic.py`, `.env.example`

**Context:** Cabinet traffic-purchase handler resolves `request.gb`, computes price, balance-check (~line 184), `subtract_user_balance` (~line 228). `subscription`, `request.gb`, `user`, `db`, `settings`, `HTTPException`, `status` in scope (HTTPException/status used at ~line 210). Cap-check BEFORE pricing/charge, for `request.gb > 0`.

- [ ] **Step 1: Read the handler region**

Read `app/cabinet/routes/subscription_modules/traffic.py` ~lines 140-230 — locate where `request.gb` + `subscription` are available, before pricing/charge. Confirm names.

- [ ] **Step 2: Insert the cap-check**

After `subscription` is resolved and BEFORE price calc / balance check (~before line 150), add:
```python
    if request.gb > 0:
        from app.database.crud.subscription import can_add_purchased_traffic

        allowed, remaining = await can_add_purchased_traffic(db, subscription.id, request.gb)
        if not allowed:
            cap = settings.get_max_purchased_traffic_gb()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    'code': 'traffic_cap',
                    'message': f'Достигнут лимит докупленного трафика ({cap} ГБ). Доступно ещё: {remaining} ГБ.',
                    'cap_gb': cap,
                    'remaining_gb': remaining,
                },
            )
```
Placement before `subtract_user_balance` (~228) → no charge on rejection.

- [ ] **Step 3: Verify import**

Run: `.venv/Scripts/python.exe -c "import app.cabinet.routes.subscription_modules.traffic; print('OK')"`

- [ ] **Step 4: Regression + final verify**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_purchased_traffic_cap.py -v` → 6 PASS.
Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q` → no NEW failures vs baseline (~29 pre-existing).

- [ ] **Step 5: .env.example**

Add near other traffic settings:
```
# Max accumulated purchased (top-up) traffic per subscription, GB. 0 = no cap. Rejects user purchases that would exceed it (admin/API top-ups are not capped).
MAX_PURCHASED_TRAFFIC_GB=0
```

- [ ] **Step 6: Commit**

```bash
git add app/cabinet/routes/subscription_modules/traffic.py .env.example
git commit -m "feat(traffic-cap): cabinet purchase cap pre-check + env doc"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] Cap default 0 (uncapped) — no prod behavior change until admin sets >0.
- [ ] `get_active_purchased_traffic_gb` sums only non-expired TrafficPurchase (source of truth, not the counter).
- [ ] Cap-check runs BEFORE charge in BOTH bot + cabinet (no charge-then-reject).
- [ ] gb==0 (switch-to-unlimited) is NOT cap-checked.
- [ ] webapi (API-token) + admin-bulk are NOT capped (trusted callers) — left untouched.
- [ ] `add_subscription_traffic` unchanged (shared primitive).
- [ ] No migration.

## Out of plan scope (follow-ups)

- WL-traffic cap (`wl_purchased_traffic_gb`) — symmetric, separate config.
- Hiding over-cap packages on the selection screen (pre-charge block is sufficient for v1).
- Admin-UI for the cap value (env-only v1).
- Concurrency hard-serialization (accepted compromise: low risk, user-vs-self).
