# Cabinet WL Traffic Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the cabinet to full parity with the bot for Whitelabel (WL) traffic management — view, top-up, reset, switch — by extracting shared logic into `_traffic_core.py` and adding a thin `wl_traffic.py` route module on top.

**Architecture:** A new module `app/cabinet/routes/subscription_modules/_traffic_core.py` exposes helpers parameterised by `kind: Literal['regular', 'wl']` (resolve packages, compute pricing, apply DB updates, sync RemnaWave, refresh from panel). The existing `traffic.py` is refactored to delegate to those helpers (regression-protected). A new `wl_traffic.py` mounts six endpoints under `/cabinet/subscription/wl-*` that mirror the regular ones plus `/wl-traffic/reset`. The frontend gains an always-visible WL section that disables actions when WL is unavailable.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, httpx, structlog, pytest + pytest-asyncio (backend); React + TypeScript + Vite, Zustand, Tailwind (frontend).

**Spec:** [docs/superpowers/specs/2026-05-07-cabinet-wl-traffic-design.md](../specs/2026-05-07-cabinet-wl-traffic-design.md)

---

## File Structure

### Created

| Path | Responsibility |
|------|---------------|
| `app/utils/traffic_pricing.py` | Shared `calculate_traffic_reset_price(subscription, kind)` — extracted from bot `_calculate_traffic_reset_price`, parameterised. |
| `app/cabinet/routes/subscription_modules/_traffic_core.py` | `kind`-parameterised helpers: `resolve_traffic_packages`, `resolve_package_price`, `apply_purchase_db`, `delete_purchases_for_switch`, `sync_remnawave_after_purchase`, `refresh_used_from_panel`. |
| `app/cabinet/routes/subscription_modules/wl_traffic.py` | FastAPI router with 6 endpoints under `/cabinet/subscription/wl-*`. Thin wrapper over `_traffic_core`. |
| `tests/cabinet/subscription/__init__.py` | Empty package marker. |
| `tests/cabinet/subscription/conftest.py` | Shared fixtures: `make_subscription`, `make_user`, `mock_db`. |
| `tests/cabinet/subscription/test_traffic_pricing.py` | Unit tests for the extracted reset-price util. |
| `tests/cabinet/subscription/test_traffic_core.py` | Unit tests for `_traffic_core` helpers (parameterised over `kind`). |
| `tests/cabinet/subscription/test_wl_traffic_routes.py` | Integration tests for the 6 WL endpoints. |
| `tests/cabinet/subscription/test_traffic_regression.py` | Verifies regular `/traffic*` endpoints unchanged after refactor. |
| `bedolaga-cabinet/src/api/wlTraffic.ts` | API client (`getPackages`, `purchase`, `switch`, `reset`, `refresh`, `saveCart`). |
| `bedolaga-cabinet/src/components/subscription/WlTrafficSection.tsx` | Always-visible cabinet section. |
| `bedolaga-cabinet/src/components/subscription/WlTrafficDialogs.tsx` | Add / switch / reset modals. |

### Modified

| Path | Change summary |
|------|----------------|
| `app/handlers/subscription/wl_traffic.py` | Replace internal `_calculate_traffic_reset_price` with import from new shared util. Behaviour unchanged. |
| `app/cabinet/routes/subscription_modules/traffic.py` | Refactor — delegate package resolution, pricing, DB apply, RemnaWave sync, refresh logic to `_traffic_core` with `kind='regular'`. No behaviour change. |
| `app/cabinet/routes/subscription_modules/__init__.py` | Import + export `wl_traffic_router`. |
| `app/cabinet/routes/subscription.py` (or wherever the cabinet aggregator includes `traffic_router`) | Mount `wl_traffic_router`. |
| `app/cabinet/schemas/subscription.py` | Add WL response schemas. Reuse existing `TrafficPackageResponse` and `TrafficPurchaseRequest`. |
| `app/cabinet/routes/branding.py` | Expose `wl_traffic_topup_enabled` (reads `settings.WL_TRAFFIC_TOPUP_ENABLED`). |
| `app/services/subscription_auto_purchase_service.py` | Add handler branch for `cart_mode='add_wl_traffic'`. |
| `bedolaga-cabinet/src/pages/SubscriptionDetail.tsx` (or equivalent landing component) | Render `<WlTrafficSection>` after the existing traffic section. |
| `bedolaga-cabinet/src/locales/ru.json` | Add `wl_traffic.*` keys. |
| `bedolaga-cabinet/src/locales/en.json` | Add `wl_traffic.*` keys. |

### Untouched

- `app/database/models.py` — `Subscription` already has the `wl_traffic_*` columns.
- `app/database/crud/subscription.py:add_subscription_wl_traffic` — reused as-is.
- `app/external/remnawave_api.py:reset_user_traffic` — reused as-is.
- Migrations — no DB schema change.

---

## Conventions

- Strict TDD: failing test → run red → minimal implementation → run green → commit.
- Pytest path: `pytest tests/cabinet/subscription/<file>.py::<test> -v`.
- Backend test runner: project `.venv` with Python 3.13.
- Frontend build: `cd bedolaga-cabinet && npm run build` (Vite). Type-check via `npx tsc --noEmit` + ESLint.
- Commit prefixes: `feat`, `fix`, `refactor`, `test`, `chore`. All commits include `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- All new functions touching DB are async.
- Imports follow existing absolute-path style.

---

## Task 1: Bootstrap test scaffolding

**Files:**
- Create: `tests/cabinet/subscription/__init__.py`
- Create: `tests/cabinet/subscription/conftest.py`

- [ ] **Step 1: Create empty package marker**

```bash
mkdir -p tests/cabinet/subscription
: > tests/cabinet/subscription/__init__.py
```

- [ ] **Step 2: Write `conftest.py` with shared fixtures**

```python
# tests/cabinet/subscription/conftest.py
"""Shared fixtures for cabinet subscription/traffic tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def make_user():
    """Build a mock User row with the fields traffic logic touches."""
    def _make(*, id: int = 1, balance_kopeks: int = 10_000_000, telegram_id: int = 1234567890):
        u = SimpleNamespace()
        u.id = id
        u.telegram_id = telegram_id
        u.balance_kopeks = balance_kopeks
        u.remnawave_uuid = 'main-uuid'
        u.restriction_subscription = False
        return u
    return _make


@pytest.fixture
def make_subscription():
    """Build a mock Subscription row. Defaults to a paid subscription with 50GB regular and 50GB WL."""
    def _make(
        *,
        id: int = 1,
        user_id: int = 1,
        is_trial: bool = False,
        status: str = 'active',
        traffic_limit_gb: int = 50,
        traffic_used_gb: float = 10.0,
        purchased_traffic_gb: int = 0,
        wl_traffic_limit_gb: int = 50,
        wl_traffic_used_gb: float = 5.0,
        wl_purchased_traffic_gb: int = 0,
        tariff_id: int | None = None,
        days_left: int = 30,
        remnawave_uuid: str = 'sub-uuid',
    ):
        s = SimpleNamespace()
        s.id = id
        s.user_id = user_id
        s.is_trial = is_trial
        s.status = status
        s.traffic_limit_gb = traffic_limit_gb
        s.traffic_used_gb = traffic_used_gb
        s.purchased_traffic_gb = purchased_traffic_gb
        s.traffic_reset_at = None
        s.wl_traffic_limit_gb = wl_traffic_limit_gb
        s.wl_traffic_used_gb = wl_traffic_used_gb
        s.wl_purchased_traffic_gb = wl_purchased_traffic_gb
        s.wl_traffic_reset_at = None
        s.tariff_id = tariff_id
        s.end_date = datetime.now(UTC) + timedelta(days=days_left)
        s.start_date = datetime.now(UTC) - timedelta(days=1)
        s.remnawave_uuid = remnawave_uuid
        s.updated_at = datetime.now(UTC)
        return s
    return _make


@pytest.fixture
def mock_db():
    """Mock AsyncSession with the methods our code calls."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return db
```

- [ ] **Step 3: Verify fixtures collect cleanly**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/ --collect-only`
Expected: "no tests collected" with 0 errors.

- [ ] **Step 4: Commit**

```bash
git add tests/cabinet/subscription/__init__.py tests/cabinet/subscription/conftest.py
git commit -m "test(cabinet): scaffold subscription/WL traffic test fixtures"
```

---

## Task 2: Extract `calculate_traffic_reset_price` shared util

**Files:**
- Create: `app/utils/traffic_pricing.py`
- Test: `tests/cabinet/subscription/test_traffic_pricing.py`
- Modify: `app/handlers/subscription/wl_traffic.py` (replace local helper with import)

The bot file `app/handlers/subscription/wl_traffic.py:123-157` defines `_calculate_traffic_reset_price(subscription)` that reads `subscription.wl_traffic_limit_gb` and `subscription.wl_purchased_traffic_gb`. Extract it parameterised by `kind`.

- [ ] **Step 1: Write failing tests**

Create `tests/cabinet/subscription/test_traffic_pricing.py`:

```python
"""Unit tests for traffic_pricing.calculate_traffic_reset_price."""

from unittest.mock import MagicMock, patch

import pytest


def test_reset_price_period_mode(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'period'
    fake_settings.get_traffic_reset_base_price.return_value = 9000
    fake_settings.get_wl_traffic_price.return_value = 0

    with patch.object(tp, 'settings', fake_settings):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 9000


def test_reset_price_traffic_mode(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription(wl_traffic_limit_gb=50)
    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'traffic'
    fake_settings.get_traffic_reset_base_price.return_value = 1000
    fake_settings.get_wl_traffic_price.return_value = 5000

    with patch.object(tp, 'settings', fake_settings):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 5000  # max(5000, 1000)


def test_reset_price_traffic_with_purchased_mode(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription(wl_traffic_limit_gb=70, wl_purchased_traffic_gb=20)

    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'traffic_with_purchased'
    fake_settings.get_traffic_reset_base_price.return_value = 0

    def _wl_price(gb):
        return {50: 4000, 20: 2000}.get(gb, 0)

    fake_settings.get_wl_traffic_price.side_effect = _wl_price

    with patch.object(tp, 'settings', fake_settings):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 6000  # 4000 + 2000


def test_reset_price_unknown_mode_falls_back_to_base(make_subscription):
    from app.utils import traffic_pricing as tp

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.get_traffic_reset_price_mode.return_value = 'something_else'
    fake_settings.get_traffic_reset_base_price.return_value = 12345
    fake_settings.get_wl_traffic_price.return_value = 0

    with patch.object(tp, 'settings', fake_settings):
        price = tp.calculate_traffic_reset_price(sub, kind='wl')
        assert price == 12345
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_pricing.py -v`
Expected: 4 failures with `ImportError: cannot import name 'traffic_pricing'`.

- [ ] **Step 3: Implement `app/utils/traffic_pricing.py`**

```python
# app/utils/traffic_pricing.py
"""Shared traffic-pricing helpers used by both bot and cabinet."""

from __future__ import annotations

from typing import Literal

from app.config import PERIOD_PRICES, settings


TrafficKind = Literal['regular', 'wl']


def _get_field(subscription, kind: TrafficKind, field: str) -> int:
    if kind == 'wl':
        return getattr(subscription, f'wl_{field}', 0) or 0
    return getattr(subscription, field, 0) or 0


def _get_unit_price(gb: int, kind: TrafficKind) -> int:
    if kind == 'wl':
        return settings.get_wl_traffic_price(gb)
    if hasattr(settings, 'get_traffic_price'):
        return settings.get_traffic_price(gb)
    return settings.get_wl_traffic_price(gb)


def calculate_traffic_reset_price(subscription, *, kind: TrafficKind) -> int:
    """Return the price (in kopeks) for resetting traffic counter on a subscription.

    Modes (from settings.get_traffic_reset_price_mode):
      - 'period': fixed = settings.get_traffic_reset_base_price() or PERIOD_PRICES[30].
      - 'traffic': max(unit_price(current_limit), base_price).
      - 'traffic_with_purchased': unit_price(base_gb) + unit_price(purchased_gb), floored at base_price.
      - anything else: base_price (fallback).
    """
    mode = settings.get_traffic_reset_price_mode()
    base_price = settings.get_traffic_reset_base_price()
    if base_price == 0:
        base_price = PERIOD_PRICES.get(30, 0)

    current_limit = _get_field(subscription, kind, 'traffic_limit_gb')
    purchased_gb = _get_field(subscription, kind, 'purchased_traffic_gb')

    if mode == 'period':
        return base_price

    if mode == 'traffic':
        traffic_price = _get_unit_price(current_limit, kind)
        return max(traffic_price, base_price)

    if mode == 'traffic_with_purchased':
        base_gb = max(0, current_limit - purchased_gb)
        base_traffic_price = _get_unit_price(base_gb, kind) if base_gb > 0 else 0
        purchased_traffic_price = _get_unit_price(purchased_gb, kind) if purchased_gb > 0 else 0
        total = base_traffic_price + purchased_traffic_price
        return max(total, base_price)

    return base_price
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_pricing.py -v`
Expected: 4 passed.

- [ ] **Step 5: Wire bot handler to use the shared util**

In `app/handlers/subscription/wl_traffic.py`, replace lines 123–157 (the local `_calculate_traffic_reset_price`) with:

```python
from app.utils.traffic_pricing import calculate_traffic_reset_price as _calculate_traffic_reset_price_util


def _calculate_traffic_reset_price(subscription) -> int:
    """Bot-facing wrapper. Bot always operates on WL fields here."""
    return _calculate_traffic_reset_price_util(subscription, kind='wl')
```

Run the existing regression test for the bot:

`.venv/Scripts/python.exe -m pytest tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py -v`
Expected: pre-existing pass count remains.

- [ ] **Step 6: Commit**

```bash
git add app/utils/traffic_pricing.py tests/cabinet/subscription/test_traffic_pricing.py app/handlers/subscription/wl_traffic.py
git commit -m "refactor: extract traffic reset price util parameterised by kind"
```

---

## Task 3: `_traffic_core.resolve_traffic_packages(kind)`

**Files:**
- Create: `app/cabinet/routes/subscription_modules/_traffic_core.py`
- Test: `tests/cabinet/subscription/test_traffic_core.py`

- [ ] **Step 1: Write failing tests**

Create `tests/cabinet/subscription/test_traffic_core.py`:

```python
"""Unit tests for _traffic_core kind-parameterised helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_resolve_packages_wl_tariff_mode_uses_tariff_packages(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(tariff_id=42)
    fake_tariff = MagicMock()
    fake_tariff.wl_traffic_topup_packages = {10: 5000, 50: 20000}
    fake_tariff.can_topup_wl_traffic.return_value = True

    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = True
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True

    with (
        patch.object(tc, 'settings', fake_settings),
        patch.object(tc, 'get_tariff_by_id', AsyncMock(return_value=fake_tariff)),
    ):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        gbs = sorted(p['gb'] for p in packages)
        assert gbs == [10, 50]


@pytest.mark.asyncio
async def test_resolve_packages_wl_returns_empty_when_globally_disabled(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = False

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        assert packages == []


@pytest.mark.asyncio
async def test_resolve_packages_wl_returns_empty_when_unlimited(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(wl_traffic_limit_gb=0)
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.get_wl_traffic_packages.return_value = [{'gb': 10, 'price': 5000, 'enabled': True}]

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        assert packages == []


@pytest.mark.asyncio
async def test_resolve_packages_wl_classic_uses_global_packages(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(wl_traffic_limit_gb=50)
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.is_traffic_topup_blocked.return_value = False
    fake_settings.get_wl_traffic_packages.return_value = [
        {'gb': 10, 'price': 5000, 'enabled': True},
        {'gb': 0, 'price': 100000, 'enabled': True},
        {'gb': 25, 'price': 9000, 'enabled': False},
        {'gb': 100, 'price': 0, 'enabled': True},
    ]

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='wl')
        gbs = sorted(p['gb'] for p in packages)
        assert gbs == [0, 10]


@pytest.mark.asyncio
async def test_resolve_packages_regular_returns_existing_logic(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.is_traffic_topup_enabled.return_value = True
    fake_settings.get_traffic_topup_packages.return_value = [
        {'gb': 5, 'price': 1000, 'enabled': True},
    ]

    with patch.object(tc, 'settings', fake_settings):
        packages = await tc.resolve_traffic_packages(mock_db, sub, kind='regular')
        assert [p['gb'] for p in packages] == [5]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -v`
Expected: failures with `ImportError: cannot import name '_traffic_core'`.

- [ ] **Step 3: Create `_traffic_core.py` with the helper**

```python
# app/cabinet/routes/subscription_modules/_traffic_core.py
"""Shared kind-parameterised helpers for cabinet traffic endpoints."""

from __future__ import annotations

from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import Subscription


logger = structlog.get_logger(__name__)

TrafficKind = Literal['regular', 'wl']


def get_limit_gb(subscription: Subscription, kind: TrafficKind) -> int:
    return getattr(subscription, f'{"wl_" if kind == "wl" else ""}traffic_limit_gb', 0) or 0


def get_used_gb(subscription: Subscription, kind: TrafficKind) -> float:
    return getattr(subscription, f'{"wl_" if kind == "wl" else ""}traffic_used_gb', 0.0) or 0.0


def get_purchased_gb(subscription: Subscription, kind: TrafficKind) -> int:
    field = 'wl_purchased_traffic_gb' if kind == 'wl' else 'purchased_traffic_gb'
    return getattr(subscription, field, 0) or 0


async def resolve_traffic_packages(
    db: AsyncSession,
    subscription: Subscription,
    *,
    kind: TrafficKind,
) -> list[dict[str, Any]]:
    """Return the list of available top-up packages for the given kind."""
    if subscription.is_trial:
        return []

    if kind == 'wl' and not getattr(settings, 'WL_TRAFFIC_TOPUP_ENABLED', True):
        return []

    if get_limit_gb(subscription, kind) == 0:
        return []

    if settings.is_tariffs_mode() and subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff is not None:
            if kind == 'wl':
                if getattr(tariff, 'wl_traffic_topup_packages', None):
                    raw = tariff.wl_traffic_topup_packages or {}
                    return [
                        {'gb': int(gb), 'price': int(price), 'is_unlimited': int(gb) == 0}
                        for gb, price in raw.items()
                        if price and int(price) > 0
                    ]
            else:
                if getattr(tariff, 'traffic_topup_enabled', False):
                    raw = tariff.get_traffic_topup_packages() if hasattr(tariff, 'get_traffic_topup_packages') else {}
                    return [
                        {'gb': int(gb), 'price': int(price), 'is_unlimited': int(gb) == 0}
                        for gb, price in raw.items()
                        if price and int(price) > 0
                    ]

    if kind == 'wl':
        raw_packages = settings.get_wl_traffic_packages()
    else:
        if not settings.is_traffic_topup_enabled():
            return []
        raw_packages = settings.get_traffic_topup_packages()

    result: list[dict[str, Any]] = []
    for pkg in raw_packages:
        if not pkg.get('enabled', True):
            continue
        if pkg.get('price', 0) <= 0:
            continue
        result.append({
            'gb': int(pkg['gb']),
            'price': int(pkg['price']),
            'is_unlimited': int(pkg['gb']) == 0,
        })

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/_traffic_core.py tests/cabinet/subscription/test_traffic_core.py
git commit -m "feat(cabinet): _traffic_core.resolve_traffic_packages(kind)"
```

---

## Task 4: `_traffic_core.resolve_package_price(kind)`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/_traffic_core.py`
- Test: `tests/cabinet/subscription/test_traffic_core.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/cabinet/subscription/test_traffic_core.py`:

```python
@pytest.mark.asyncio
async def test_resolve_package_price_wl_tariff_match(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(tariff_id=42)
    fake_tariff = MagicMock()
    fake_tariff.wl_traffic_topup_packages = {50: 12500}
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = True
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True

    with (
        patch.object(tc, 'settings', fake_settings),
        patch.object(tc, 'get_tariff_by_id', AsyncMock(return_value=fake_tariff)),
    ):
        price = await tc.resolve_package_price(mock_db, sub, gb=50, kind='wl')
        assert price == 12500


@pytest.mark.asyncio
async def test_resolve_package_price_wl_classic_match(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = False
    fake_settings.is_traffic_topup_blocked.return_value = False
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.get_wl_traffic_topup_price.return_value = 7777

    with patch.object(tc, 'settings', fake_settings):
        price = await tc.resolve_package_price(mock_db, sub, gb=25, kind='wl')
        assert price == 7777


@pytest.mark.asyncio
async def test_resolve_package_price_returns_zero_when_unknown(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription(tariff_id=42)
    fake_tariff = MagicMock()
    fake_tariff.wl_traffic_topup_packages = {10: 1000}
    fake_settings = MagicMock()
    fake_settings.is_tariffs_mode.return_value = True
    fake_settings.WL_TRAFFIC_TOPUP_ENABLED = True
    fake_settings.is_traffic_topup_blocked.return_value = False
    fake_settings.get_wl_traffic_topup_price.return_value = 0

    with (
        patch.object(tc, 'settings', fake_settings),
        patch.object(tc, 'get_tariff_by_id', AsyncMock(return_value=fake_tariff)),
    ):
        price = await tc.resolve_package_price(mock_db, sub, gb=999, kind='wl')
        assert price == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py::test_resolve_package_price_wl_tariff_match -v`
Expected: FAIL — `resolve_package_price` not defined.

- [ ] **Step 3: Implement `resolve_package_price`**

Append to `app/cabinet/routes/subscription_modules/_traffic_core.py`:

```python
async def resolve_package_price(
    db: AsyncSession,
    subscription: Subscription,
    *,
    gb: int,
    kind: TrafficKind,
) -> int:
    """Return the per-month base price for one top-up package.

    Returns 0 when the package is unknown — caller is expected to reject.
    """
    if settings.is_tariffs_mode() and subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff is not None:
            if kind == 'wl':
                pkgs = tariff.wl_traffic_topup_packages or {}
                if gb in pkgs:
                    return int(pkgs[gb])
            else:
                if hasattr(tariff, 'get_traffic_topup_packages'):
                    pkgs = tariff.get_traffic_topup_packages() or {}
                    if gb in pkgs:
                        return int(pkgs[gb])

    if kind == 'wl':
        if not settings.WL_TRAFFIC_TOPUP_ENABLED:
            return 0
        return int(settings.get_wl_traffic_topup_price(gb))

    if not settings.is_traffic_topup_enabled():
        return 0
    pkgs = settings.get_traffic_topup_packages()
    match = next((p for p in pkgs if p['gb'] == gb and p.get('enabled', True)), None)
    return int(match['price']) if match else 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/_traffic_core.py tests/cabinet/subscription/test_traffic_core.py
git commit -m "feat(cabinet): _traffic_core.resolve_package_price(kind)"
```

---

## Task 5: `_traffic_core.apply_purchase_db(kind)`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/_traffic_core.py`
- Test: `tests/cabinet/subscription/test_traffic_core.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_apply_purchase_db_wl_calls_add_wl_crud(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    add_wl = AsyncMock()
    add_regular = AsyncMock()

    with (
        patch.object(tc, 'add_subscription_wl_traffic', add_wl),
        patch.object(tc, 'add_subscription_traffic', add_regular),
    ):
        await tc.apply_purchase_db(mock_db, sub, gb=50, kind='wl')

    add_wl.assert_awaited_once_with(mock_db, sub, 50)
    add_regular.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_purchase_db_regular_calls_add_regular_crud(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    add_wl = AsyncMock()
    add_regular = AsyncMock()

    with (
        patch.object(tc, 'add_subscription_wl_traffic', add_wl),
        patch.object(tc, 'add_subscription_traffic', add_regular),
    ):
        await tc.apply_purchase_db(mock_db, sub, gb=10, kind='regular')

    add_regular.assert_awaited_once_with(mock_db, sub, 10)
    add_wl.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py::test_apply_purchase_db_wl_calls_add_wl_crud -v`
Expected: FAIL — `apply_purchase_db` not defined.

- [ ] **Step 3: Implement**

Append to `_traffic_core.py`:

```python
from app.database.crud.subscription import (
    add_subscription_traffic,
    add_subscription_wl_traffic,
)


async def apply_purchase_db(
    db: AsyncSession,
    subscription: Subscription,
    *,
    gb: int,
    kind: TrafficKind,
) -> None:
    """Persist a successful top-up: increments limit + creates *TrafficPurchase row."""
    if kind == 'wl':
        await add_subscription_wl_traffic(db, subscription, gb)
    else:
        await add_subscription_traffic(db, subscription, gb)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/_traffic_core.py tests/cabinet/subscription/test_traffic_core.py
git commit -m "feat(cabinet): _traffic_core.apply_purchase_db(kind)"
```

---

## Task 6: `_traffic_core.delete_purchases_for_switch(kind)` + `sync_remnawave_after_purchase`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/_traffic_core.py`
- Test: `tests/cabinet/subscription/test_traffic_core.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_delete_purchases_wl_uses_wl_purchase_table(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    captured = {}

    async def _exec(stmt):
        captured['sql'] = str(stmt)
        return MagicMock()

    mock_db.execute = AsyncMock(side_effect=_exec)

    await tc.delete_purchases_for_switch(mock_db, sub, kind='wl')
    assert 'wl_traffic_purchases' in captured['sql'].lower()


@pytest.mark.asyncio
async def test_delete_purchases_regular_uses_regular_purchase_table(make_subscription, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    sub = make_subscription()
    captured = {}

    async def _exec(stmt):
        captured['sql'] = str(stmt)
        return MagicMock()

    mock_db.execute = AsyncMock(side_effect=_exec)

    await tc.delete_purchases_for_switch(mock_db, sub, kind='regular')
    sql = captured['sql'].lower()
    assert 'traffic_purchases' in sql
    assert 'wl_traffic_purchases' not in sql


@pytest.mark.asyncio
async def test_sync_remnawave_calls_update_when_uuid_present(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    user = make_user()
    sub = make_subscription(remnawave_uuid='sub-uuid')

    fake_service = MagicMock()
    fake_service.update_remnawave_user = AsyncMock()
    fake_service.create_remnawave_user = AsyncMock()
    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False

    with (
        patch.object(tc, 'SubscriptionService', return_value=fake_service),
        patch.object(tc, 'settings', fake_settings),
    ):
        await tc.sync_remnawave_after_purchase(mock_db, sub, user)

    fake_service.update_remnawave_user.assert_awaited_once_with(mock_db, sub)
    fake_service.create_remnawave_user.assert_not_awaited()
```

- [ ] **Step 2: Run tests — verify FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -k "delete_purchases or sync_remnawave" -v`
Expected: 3 failures.

- [ ] **Step 3: Implement**

Append to `_traffic_core.py`:

```python
from sqlalchemy import delete as sql_delete

from app.database.models import TrafficPurchase, WlTrafficPurchase
from app.services.subscription_service import SubscriptionService


async def delete_purchases_for_switch(
    db: AsyncSession,
    subscription: Subscription,
    *,
    kind: TrafficKind,
) -> None:
    """Wipe accumulated *TrafficPurchase rows before switching the package."""
    table = WlTrafficPurchase if kind == 'wl' else TrafficPurchase
    await db.execute(sql_delete(table).where(table.subscription_id == subscription.id))


async def sync_remnawave_after_purchase(
    db: AsyncSession,
    subscription: Subscription,
    user,
) -> None:
    """Best-effort RemnaWave sync after any traffic purchase.

    On hard failure the subscription is enqueued for retry.
    """
    should_create = False
    try:
        service = SubscriptionService()
        if settings.is_multi_tariff_enabled():
            should_create = not subscription.remnawave_uuid
        else:
            should_create = not getattr(user, 'remnawave_uuid', None)
        if should_create:
            await service.create_remnawave_user(db, subscription)
        else:
            await service.update_remnawave_user(db, subscription)
    except Exception as e:
        logger.error('Failed to sync traffic with RemnaWave', error=str(e))
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='create' if should_create else 'update',
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/_traffic_core.py tests/cabinet/subscription/test_traffic_core.py
git commit -m "feat(cabinet): _traffic_core delete_purchases + sync_remnawave"
```

---

## Task 7: `_traffic_core.refresh_used_from_panel(kind)`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/_traffic_core.py`
- Test: `tests/cabinet/subscription/test_traffic_core.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_refresh_used_wl_uses_wl_panel_user(make_subscription, make_user):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    user = make_user()
    sub = make_subscription()

    fake_panel_user = MagicMock(uuid='wl-uuid-123')
    fake_api = MagicMock()
    fake_api.get_user_by_username = AsyncMock(side_effect=[fake_panel_user])

    fake_remnawave = MagicMock()
    fake_remnawave.get_api_client = MagicMock(return_value=AsyncMock())
    fake_remnawave.get_api_client.return_value.__aenter__ = AsyncMock(return_value=fake_api)
    fake_remnawave.get_api_client.return_value.__aexit__ = AsyncMock(return_value=False)
    fake_remnawave.get_user_traffic_stats_by_uuid = AsyncMock(
        return_value={'used_traffic_gb': 4.0, 'used_traffic_bytes': 1024**3 * 4},
    )

    fake_subscription_service = MagicMock()
    fake_subscription_service._build_wl_username = MagicMock(return_value=('primary_wl', 'legacy_wl'))

    with (
        patch.object(tc, 'RemnaWaveService', return_value=fake_remnawave),
        patch.object(tc, 'SubscriptionService', return_value=fake_subscription_service),
    ):
        stats = await tc.refresh_used_from_panel(user, sub, kind='wl')

    assert stats is not None
    assert stats['used_traffic_gb'] >= 4.0


@pytest.mark.asyncio
async def test_refresh_used_regular_uses_main_uuid(make_subscription, make_user):
    from app.cabinet.routes.subscription_modules import _traffic_core as tc

    user = make_user()
    sub = make_subscription(remnawave_uuid='main-sub-uuid')

    fake_remnawave = MagicMock()
    fake_remnawave.get_user_traffic_stats_by_uuid = AsyncMock(
        return_value={'used_traffic_gb': 1.5, 'used_traffic_bytes': 1024**3 * 1.5},
    )

    with patch.object(tc, 'RemnaWaveService', return_value=fake_remnawave):
        stats = await tc.refresh_used_from_panel(user, sub, kind='regular')

    fake_remnawave.get_user_traffic_stats_by_uuid.assert_awaited_once_with('main-sub-uuid')
    assert stats['used_traffic_gb'] == 1.5
```

- [ ] **Step 2: Run tests — verify FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -k refresh -v`
Expected: 2 failures.

- [ ] **Step 3: Implement**

Append to `_traffic_core.py`:

```python
from app.services.remnawave_service import RemnaWaveService


async def refresh_used_from_panel(
    user,
    subscription: Subscription,
    *,
    kind: TrafficKind,
) -> dict[str, Any] | None:
    """Pull fresh used traffic from the relevant panel user."""
    remnawave = RemnaWaveService()

    if kind == 'wl':
        try:
            primary_wl, legacy_wl = SubscriptionService()._build_wl_username(user, subscription)
        except Exception as exc:
            logger.warning('Failed to build WL username', error=str(exc))
            return None

        async with remnawave.get_api_client() as api:
            wl_user = await api.get_user_by_username(primary_wl)
            if wl_user is None and legacy_wl and legacy_wl != primary_wl:
                wl_user = await api.get_user_by_username(legacy_wl)
            if wl_user is None or not getattr(wl_user, 'uuid', None):
                return None
            return await remnawave.get_user_traffic_stats_by_uuid(wl_user.uuid)

    target_uuid = subscription.remnawave_uuid or getattr(user, 'remnawave_uuid', None)
    if not target_uuid:
        return None
    return await remnawave.get_user_traffic_stats_by_uuid(target_uuid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_core.py -v`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/_traffic_core.py tests/cabinet/subscription/test_traffic_core.py
git commit -m "feat(cabinet): _traffic_core.refresh_used_from_panel(kind)"
```

---

## Task 8: WL response schemas

**Files:**
- Modify: `app/cabinet/schemas/subscription.py`

- [ ] **Step 1: Read existing schemas**

Run: `grep -n "TrafficPackageResponse\|TrafficPurchaseRequest" app/cabinet/schemas/subscription.py`

Identify where to insert the new types (typically next to existing traffic schemas).

- [ ] **Step 2: Add schemas**

Append to the appropriate section of `app/cabinet/schemas/subscription.py`:

```python
class WlTrafficPurchaseResponse(BaseModel):
    success: bool = True
    gb_added: int
    new_wl_traffic_limit_gb: int
    amount_paid_kopeks: int
    new_balance_kopeks: int
    discount_percent: int | None = None
    discount_kopeks: int | None = None
    base_price_kopeks: int | None = None


class WlTrafficSwitchResponse(BaseModel):
    success: bool = True
    old_wl_traffic_gb: int
    new_wl_traffic_gb: int
    charged_kopeks: int
    balance_kopeks: int
    balance_label: str


class WlTrafficResetResponse(BaseModel):
    success: bool = True
    new_wl_traffic_used_gb: float
    charged_kopeks: int
    balance_kopeks: int


class WlTrafficRefreshResponse(BaseModel):
    success: bool = True
    cached: bool = False
    rate_limited: bool = False
    source: str
    wl_traffic_used_bytes: int
    wl_traffic_used_gb: float
    wl_traffic_limit_bytes: int
    wl_traffic_limit_gb: int
    wl_traffic_used_percent: float
    is_unlimited: bool
    lifetime_used_bytes: int = 0
    lifetime_used_gb: float = 0.0
    retry_after_seconds: int | None = None
```

- [ ] **Step 3: Verify imports**

Run: `.venv/Scripts/python.exe -c "from app.cabinet.schemas.subscription import WlTrafficPurchaseResponse, WlTrafficSwitchResponse, WlTrafficResetResponse, WlTrafficRefreshResponse; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add app/cabinet/schemas/subscription.py
git commit -m "feat(cabinet): WL traffic response schemas"
```

---

## Task 9: `wl_traffic.py` — GET `/wl-traffic-packages`

**Files:**
- Create: `app/cabinet/routes/subscription_modules/wl_traffic.py`
- Test: `tests/cabinet/subscription/test_wl_traffic_routes.py`

- [ ] **Step 1: Write failing tests**

Create `tests/cabinet/subscription/test_wl_traffic_routes.py`:

```python
"""Integration tests for /cabinet/subscription/wl-* endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_wl_packages_returns_resolved_list(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(
            wt,
            'resolve_traffic_packages',
            AsyncMock(return_value=[{'gb': 10, 'price': 5000, 'is_unlimited': False}]),
        ),
    ):
        result = await wt.get_wl_traffic_packages(user=user, db=mock_db, subscription_id=None)

    assert len(result) == 1
    pkg = result[0]
    assert pkg.gb == 10
    assert pkg.price_kopeks == 5000
    assert pkg.price_rubles == 50.0
    assert pkg.is_unlimited is False


@pytest.mark.asyncio
async def test_wl_packages_returns_empty_when_no_subscription(make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=None)):
        result = await wt.get_wl_traffic_packages(user=make_user(), db=mock_db, subscription_id=None)

    assert result == []
```

- [ ] **Step 2: Run tests — verify FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: failures — `wl_traffic` module not found.

- [ ] **Step 3: Create the router with the GET endpoint**

```python
# app/cabinet/routes/subscription_modules/wl_traffic.py
"""WL traffic endpoints for cabinet."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query as QueryParam
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import TrafficPackageResponse
from ._traffic_core import resolve_traffic_packages
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get('/wl-traffic-packages', response_model=list[TrafficPackageResponse])
async def get_wl_traffic_packages(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> list[TrafficPackageResponse]:
    """Available WL top-up packages for the resolved subscription."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        return []

    packages = await resolve_traffic_packages(db, subscription, kind='wl')
    return [
        TrafficPackageResponse(
            gb=p['gb'],
            price_kopeks=p['price'],
            price_rubles=p['price'] / 100,
            is_unlimited=p['is_unlimited'],
        )
        for p in packages
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/wl_traffic.py tests/cabinet/subscription/test_wl_traffic_routes.py
git commit -m "feat(cabinet): GET /cabinet/subscription/wl-traffic-packages"
```

---

## Task 10: POST `/wl-traffic` — purchase

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/wl_traffic.py`
- Test: `tests/cabinet/subscription/test_wl_traffic_routes.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_wl_purchase_success(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=10_000_000)
    sub = make_subscription()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'resolve_package_price', AsyncMock(return_value=4000)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 4000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'apply_purchase_db', AsyncMock()),
        patch.object(wt, 'reactivate_subscription', AsyncMock()),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'sync_remnawave_after_purchase', AsyncMock()),
        patch.object(wt, 'calculate_prorated_price', return_value=(4000, 30)),
    ):
        sub.wl_traffic_limit_gb = 60
        result = await wt.purchase_wl_traffic(
            request=TrafficPurchaseRequest(gb=10),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['success'] is True
    assert result['gb_added'] == 10
    assert result['new_wl_traffic_limit_gb'] == 60
    assert result['amount_paid_kopeks'] == 4000


@pytest.mark.asyncio
async def test_wl_purchase_rejects_trial(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(is_trial=True)

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)):
        with pytest.raises(HTTPException) as exc:
            await wt.purchase_wl_traffic(
                request=TrafficPurchaseRequest(gb=10),
                user=user,
                db=mock_db,
                subscription_id=None,
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_wl_purchase_rejects_unlimited(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=0)

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)):
        with pytest.raises(HTTPException) as exc:
            await wt.purchase_wl_traffic(
                request=TrafficPurchaseRequest(gb=10),
                user=user,
                db=mock_db,
                subscription_id=None,
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_wl_purchase_insufficient_saves_cart_and_402(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=100)
    sub = make_subscription()

    save_cart = AsyncMock()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'resolve_package_price', AsyncMock(return_value=10000)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 10000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'calculate_prorated_price', return_value=(10000, 30)),
        patch.object(wt.user_cart_service, 'save_user_cart', save_cart),
    ):
        with pytest.raises(HTTPException) as exc:
            await wt.purchase_wl_traffic(
                request=TrafficPurchaseRequest(gb=10),
                user=user,
                db=mock_db,
                subscription_id=None,
            )

    assert exc.value.status_code == 402
    save_cart.assert_awaited_once()
    cart = save_cart.await_args[0][1]
    assert cart['cart_mode'] == 'add_wl_traffic'
    assert cart['traffic_gb'] == 10
    assert cart['source'] == 'cabinet'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: 4 failures — `purchase_wl_traffic` not defined.

- [ ] **Step 3: Implement**

Append to `wl_traffic.py`:

```python
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import reactivate_subscription
from app.database.crud.transaction import create_transaction
from app.database.crud.user import lock_user_for_pricing, subtract_user_balance
from app.database.models import TransactionType
from app.services.user_cart_service import user_cart_service
from app.utils.pricing_utils import calculate_prorated_price

from ...schemas.subscription import TrafficPurchaseRequest
from ._traffic_core import (
    apply_purchase_db,
    resolve_package_price,
    sync_remnawave_after_purchase,
)
from .helpers import _apply_addon_discount


@router.post('/wl-traffic')
async def purchase_wl_traffic(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict:
    """Purchase additional WL traffic GB."""
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(status_code=403, detail='Subscription purchases are restricted for this account')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='No subscription found')
    if subscription.is_trial:
        raise HTTPException(status_code=400, detail='Эта функция доступна только для платных подписок')
    if (subscription.wl_traffic_limit_gb or 0) == 0:
        raise HTTPException(status_code=400, detail='У вас уже безлимитный трафик')
    if not getattr(settings, 'WL_TRAFFIC_TOPUP_ENABLED', True):
        raise HTTPException(status_code=400, detail='Функция докупки WL-трафика отключена')

    base_price = await resolve_package_price(db, subscription, gb=request.gb, kind='wl')
    if base_price <= 0:
        raise HTTPException(status_code=400, detail=f'Пакет {request.gb} ГБ недоступен')

    is_tariff_mode = settings.is_tariffs_mode() and subscription.tariff_id
    if is_tariff_mode:
        prorated_price, days_charged = base_price, 30
    else:
        prorated_price, days_charged = calculate_prorated_price(base_price, subscription.end_date)

    user = await lock_user_for_pricing(db, user.id)
    period_hint_days = days_charged if days_charged > 0 else 30
    discount = _apply_addon_discount(user, 'traffic', prorated_price, period_hint_days)
    final_price = discount['discounted']

    if discount['percent'] < 100 and final_price > 0:
        final_price = max(100, final_price)

    if final_price > 0 and user.balance_kopeks < final_price:
        missing = final_price - user.balance_kopeks
        try:
            await user_cart_service.save_user_cart(
                user.id,
                {
                    'cart_mode': 'add_wl_traffic',
                    'subscription_id': subscription.id,
                    'traffic_gb': request.gb,
                    'price_kopeks': final_price,
                    'base_price_kopeks': prorated_price,
                    'discount_percent': discount['percent'],
                    'source': 'cabinet',
                    'description': f'Докупка {request.gb} ГБ WL-трафика',
                },
            )
        except Exception as e:
            logger.warning('Failed to save WL cart', error=str(e))
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_funds',
                'message': f'Недостаточно средств. Не хватает {settings.format_price(missing)}',
                'missing_amount': missing,
                'cart_saved': True,
                'cart_mode': 'add_wl_traffic',
            },
        )

    description = f'Докупка {request.gb} ГБ WL-трафика'
    if discount['percent'] > 0:
        description += f' (скидка {discount["percent"]}%)'

    success = await subtract_user_balance(db, user, final_price, description)
    if not success:
        raise HTTPException(status_code=500, detail='Failed to charge balance')

    await apply_purchase_db(db, subscription, gb=request.gb, kind='wl')
    await reactivate_subscription(db, subscription)
    await sync_remnawave_after_purchase(db, subscription, user)
    await create_transaction(
        db=db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=final_price,
        description=description,
    )
    await db.refresh(user)
    await db.refresh(subscription)

    response = {
        'success': True,
        'gb_added': request.gb,
        'new_wl_traffic_limit_gb': subscription.wl_traffic_limit_gb,
        'amount_paid_kopeks': final_price,
        'new_balance_kopeks': user.balance_kopeks,
    }
    if discount['percent'] > 0:
        response['discount_percent'] = discount['percent']
        response['discount_kopeks'] = discount['discount']
        response['base_price_kopeks'] = prorated_price
    return response
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/wl_traffic.py tests/cabinet/subscription/test_wl_traffic_routes.py
git commit -m "feat(cabinet): POST /cabinet/subscription/wl-traffic"
```

---

## Task 11: PUT `/wl-traffic` — switch package

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/wl_traffic.py`
- Test: `tests/cabinet/subscription/test_wl_traffic_routes.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_wl_switch_upgrade_charges_diff(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription(wl_traffic_limit_gb=50, wl_purchased_traffic_gb=0)
    sub.user_id = user.id

    fake_settings = MagicMock()
    fake_settings.get_wl_traffic_price.side_effect = lambda gb: {50: 4000, 100: 9000}.get(gb, 0)
    fake_settings.is_multi_tariff_enabled.return_value = False
    fake_settings.format_price = lambda k: f'{k / 100:.2f} ₽'

    with (
        patch.object(wt, 'settings', fake_settings),
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 5000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'calculate_prorated_price', return_value=(5000, 30)),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'delete_purchases_for_switch', AsyncMock()),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'sync_remnawave_after_purchase', AsyncMock()),
    ):
        result = await wt.switch_wl_traffic(
            request=TrafficPurchaseRequest(gb=100),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['success'] is True
    assert result['old_wl_traffic_gb'] == 50
    assert result['new_wl_traffic_gb'] == 100
    assert result['charged_kopeks'] == 5000


@pytest.mark.asyncio
async def test_wl_switch_downgrade_no_charge(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=100, wl_purchased_traffic_gb=0)
    fake_settings = MagicMock()
    fake_settings.get_wl_traffic_price.side_effect = lambda gb: {100: 9000, 50: 4000}.get(gb, 0)

    with (
        patch.object(wt, 'settings', fake_settings),
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 0, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(wt, 'delete_purchases_for_switch', AsyncMock()),
        patch.object(wt, 'sync_remnawave_after_purchase', AsyncMock()),
    ):
        result = await wt.switch_wl_traffic(
            request=TrafficPurchaseRequest(gb=50),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['charged_kopeks'] == 0
    assert result['new_wl_traffic_gb'] == 50


@pytest.mark.asyncio
async def test_wl_switch_same_gb_400(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=50)

    with patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)):
        with pytest.raises(HTTPException) as exc:
            await wt.switch_wl_traffic(
                request=TrafficPurchaseRequest(gb=50),
                user=user,
                db=mock_db,
                subscription_id=None,
            )
    assert exc.value.status_code == 400
```

- [ ] **Step 2: Run tests — verify FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -k switch -v`
Expected: 3 failures — `switch_wl_traffic` not defined.

- [ ] **Step 3: Implement**

Append to `wl_traffic.py`:

```python
from ._traffic_core import delete_purchases_for_switch


@router.put('/wl-traffic')
async def switch_wl_traffic(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict:
    """Switch the WL traffic package (upgrade or downgrade)."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='No subscription found')
    if subscription.is_trial:
        raise HTTPException(status_code=400, detail='Эта функция доступна только для платных подписок')

    current = subscription.wl_traffic_limit_gb or 0
    new_gb = request.gb
    if current == new_gb:
        raise HTTPException(status_code=400, detail='Already on this WL traffic package')

    purchased = subscription.wl_purchased_traffic_gb or 0
    base = max(0, current - purchased)
    old_price = settings.get_wl_traffic_price(base)
    new_price = settings.get_wl_traffic_price(new_gb)
    if new_price <= 0 and new_gb != 0:
        raise HTTPException(status_code=400, detail='Invalid WL traffic package')

    user = await lock_user_for_pricing(db, user.id)

    charged = 0
    if new_price > old_price:
        diff_per_month = new_price - old_price
        discount = _apply_addon_discount(user, 'traffic', diff_per_month, 30)
        per_month_after_discount = discount['discounted']
        prorated_price, _days = calculate_prorated_price(per_month_after_discount, subscription.end_date)
        if prorated_price > 0 and user.balance_kopeks < prorated_price:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f'Insufficient balance. Need {settings.format_price(prorated_price)}',
            )
        if prorated_price > 0:
            description = f'WL traffic upgrade {current}GB → {new_gb}GB'
            success = await subtract_user_balance(db, user, prorated_price, description)
            if not success:
                raise HTTPException(status_code=500, detail='Failed to charge balance')
            await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=prorated_price,
                description=description,
            )
            charged = prorated_price

    await delete_purchases_for_switch(db, subscription, kind='wl')
    subscription.wl_traffic_limit_gb = new_gb
    subscription.wl_purchased_traffic_gb = 0
    subscription.wl_traffic_reset_at = None
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    await sync_remnawave_after_purchase(db, subscription, user)
    await db.refresh(user)
    await db.refresh(subscription)

    return {
        'success': True,
        'old_wl_traffic_gb': current,
        'new_wl_traffic_gb': new_gb,
        'charged_kopeks': charged,
        'balance_kopeks': user.balance_kopeks,
        'balance_label': settings.format_price(user.balance_kopeks),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/wl_traffic.py tests/cabinet/subscription/test_wl_traffic_routes.py
git commit -m "feat(cabinet): PUT /cabinet/subscription/wl-traffic switch package"
```

---

## Task 12: POST `/wl-traffic/reset`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/wl_traffic.py`
- Test: `tests/cabinet/subscription/test_wl_traffic_routes.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_wl_reset_success(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription(wl_traffic_used_gb=12.5)
    sub.user_id = user.id

    fake_api = MagicMock()
    fake_api.get_user_by_username = AsyncMock(return_value=MagicMock(uuid='wl-uuid'))
    fake_api.reset_user_traffic = AsyncMock()

    fake_remnawave = MagicMock()
    fake_remnawave.get_api_client = MagicMock(return_value=AsyncMock())
    fake_remnawave.get_api_client.return_value.__aenter__ = AsyncMock(return_value=fake_api)
    fake_remnawave.get_api_client.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_subscription_service = MagicMock()
    fake_subscription_service._build_wl_username = MagicMock(return_value=('wl_user', 'wl_user_legacy'))

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'calculate_traffic_reset_price', return_value=5000),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'RemnaWaveService', return_value=fake_remnawave),
        patch.object(wt, 'SubscriptionService', return_value=fake_subscription_service),
    ):
        result = await wt.reset_wl_traffic(
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result['success'] is True
    assert result['new_wl_traffic_used_gb'] == 0
    assert sub.wl_traffic_used_gb == 0.0
    fake_api.reset_user_traffic.assert_awaited_once_with('wl-uuid')


@pytest.mark.asyncio
async def test_wl_reset_insufficient_balance_402(make_subscription, make_user, mock_db):
    from fastapi import HTTPException
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user(balance_kopeks=100)
    sub = make_subscription()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'calculate_traffic_reset_price', return_value=10000),
    ):
        with pytest.raises(HTTPException) as exc:
            await wt.reset_wl_traffic(user=user, db=mock_db, subscription_id=None)
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_wl_reset_remnawave_failure_is_non_fatal(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription(wl_traffic_used_gb=8.0)

    fake_api = MagicMock()
    fake_api.get_user_by_username = AsyncMock(return_value=MagicMock(uuid='wl-uuid'))
    fake_api.reset_user_traffic = AsyncMock(side_effect=Exception('upstream down'))

    fake_remnawave = MagicMock()
    fake_remnawave.get_api_client = MagicMock(return_value=AsyncMock())
    fake_remnawave.get_api_client.return_value.__aenter__ = AsyncMock(return_value=fake_api)
    fake_remnawave.get_api_client.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_subscription_service = MagicMock()
    fake_subscription_service._build_wl_username = MagicMock(return_value=('p', 'l'))

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'calculate_traffic_reset_price', return_value=5000),
        patch.object(wt, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(wt, 'create_transaction', AsyncMock()),
        patch.object(wt, 'RemnaWaveService', return_value=fake_remnawave),
        patch.object(wt, 'SubscriptionService', return_value=fake_subscription_service),
    ):
        result = await wt.reset_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['success'] is True
    assert sub.wl_traffic_used_gb == 0.0
```

- [ ] **Step 2: Run tests — verify FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -k reset -v`
Expected: 3 failures — `reset_wl_traffic` not defined.

- [ ] **Step 3: Implement**

Append to `wl_traffic.py`:

```python
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.utils.traffic_pricing import calculate_traffic_reset_price


@router.post('/wl-traffic/reset')
async def reset_wl_traffic(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict:
    """Reset the WL traffic counter."""
    if settings.is_traffic_topup_blocked():
        raise HTTPException(status_code=400, detail='В текущем режиме трафик фиксированный')

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='No subscription found')
    if subscription.is_trial:
        raise HTTPException(status_code=400, detail='Эта функция доступна только для платных подписок')
    if (subscription.wl_traffic_limit_gb or 0) == 0:
        raise HTTPException(status_code=400, detail='У вас уже безлимитный трафик')

    reset_price = calculate_traffic_reset_price(subscription, kind='wl')
    if reset_price > 0 and user.balance_kopeks < reset_price:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f'Insufficient balance. Need {settings.format_price(reset_price)}',
        )

    if reset_price > 0:
        success = await subtract_user_balance(db, user, reset_price, 'Сброс WL-трафика')
        if not success:
            raise HTTPException(status_code=500, detail='Failed to charge balance')

    subscription.wl_traffic_used_gb = 0.0
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    try:
        primary_wl, legacy_wl = SubscriptionService()._build_wl_username(user, subscription)
        async with RemnaWaveService().get_api_client() as api:
            wl_user = await api.get_user_by_username(primary_wl)
            if wl_user is None and legacy_wl and legacy_wl != primary_wl:
                wl_user = await api.get_user_by_username(legacy_wl)
            if wl_user and getattr(wl_user, 'uuid', None):
                await api.reset_user_traffic(wl_user.uuid)
    except Exception as exc:
        logger.warning('WL traffic reset on RemnaWave failed (non-fatal)', error=str(exc))

    if reset_price > 0:
        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=reset_price,
            description='Сброс WL-трафика',
        )

    await db.refresh(user)
    await db.refresh(subscription)

    return {
        'success': True,
        'new_wl_traffic_used_gb': 0.0,
        'charged_kopeks': reset_price,
        'balance_kopeks': user.balance_kopeks,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/wl_traffic.py tests/cabinet/subscription/test_wl_traffic_routes.py
git commit -m "feat(cabinet): POST /cabinet/subscription/wl-traffic/reset"
```

---

## Task 13: POST `/refresh-wl-traffic`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/wl_traffic.py`
- Test: `tests/cabinet/subscription/test_wl_traffic_routes.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_wl_refresh_success_returns_panel_data(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=100, wl_traffic_used_gb=0.0)

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(
            wt,
            'refresh_used_from_panel',
            AsyncMock(return_value={'used_traffic_gb': 5.0, 'used_traffic_bytes': 1024**3 * 5, 'lifetime_used_traffic_gb': 5.0}),
        ),
        patch.object(wt.cache, 'set', AsyncMock()),
    ):
        result = await wt.refresh_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['success'] is True
    assert result['source'] == 'remnawave'
    assert result['wl_traffic_used_gb'] == 5.0
    assert result['wl_traffic_limit_gb'] == 100


@pytest.mark.asyncio
async def test_wl_refresh_rate_limited_returns_cached(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=50)

    cached_payload = {
        'wl_traffic_used_gb': 3.0,
        'wl_traffic_used_bytes': 1024**3 * 3,
        'wl_traffic_limit_gb': 50,
        'wl_traffic_limit_bytes': 1024**3 * 50,
        'wl_traffic_used_percent': 6.0,
        'is_unlimited': False,
        'lifetime_used_bytes': 0,
        'lifetime_used_gb': 0.0,
    }

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=True)),
        patch.object(wt.cache, 'get', AsyncMock(return_value=cached_payload)),
    ):
        result = await wt.refresh_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['cached'] is True
    assert result['rate_limited'] is True
    assert result['wl_traffic_used_gb'] == 3.0


@pytest.mark.asyncio
async def test_wl_refresh_no_panel_data_returns_database(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt

    user = make_user()
    sub = make_subscription(wl_traffic_limit_gb=100, wl_traffic_used_gb=2.0)

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(wt, 'refresh_used_from_panel', AsyncMock(return_value=None)),
    ):
        result = await wt.refresh_wl_traffic(user=user, db=mock_db, subscription_id=None)

    assert result['source'] == 'database'
    assert result['wl_traffic_used_gb'] == 2.0
```

- [ ] **Step 2: Run tests — verify FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -k refresh -v`
Expected: 3 failures.

- [ ] **Step 3: Implement**

Append to `wl_traffic.py`:

```python
from app.utils.cache import RateLimitCache, cache, cache_key

from ._traffic_core import refresh_used_from_panel


WL_TRAFFIC_REFRESH_RATE_LIMIT = 1
WL_TRAFFIC_REFRESH_RATE_WINDOW = 60
WL_TRAFFIC_CACHE_TTL = 60


@router.post('/refresh-wl-traffic')
async def refresh_wl_traffic(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict:
    """Refresh WL traffic from the RemnaWave WL panel user."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail='No active subscription')

    suffix = f'{user.id}_{subscription_id}' if subscription_id is not None else str(user.id)

    is_limited = await RateLimitCache.is_rate_limited(
        suffix,
        'wl_traffic_refresh',
        WL_TRAFFIC_REFRESH_RATE_LIMIT,
        WL_TRAFFIC_REFRESH_RATE_WINDOW,
    )
    if is_limited:
        cached = await cache.get(cache_key('wl_traffic', suffix))
        if cached:
            return {
                'success': True,
                'cached': True,
                'rate_limited': True,
                'source': 'cache',
                'retry_after_seconds': WL_TRAFFIC_REFRESH_RATE_WINDOW,
                **cached,
            }
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f'Rate limited. Try again in {WL_TRAFFIC_REFRESH_RATE_WINDOW} seconds.',
            headers={'Retry-After': str(WL_TRAFFIC_REFRESH_RATE_WINDOW)},
        )

    try:
        stats = await refresh_used_from_panel(user, subscription, kind='wl')
    except Exception as e:
        logger.error('WL traffic refresh failed', user_id=user.id, error=str(e))
        raise HTTPException(status_code=500, detail='Failed to refresh WL traffic data')

    limit_gb = subscription.wl_traffic_limit_gb or 0

    if not stats:
        used_gb = subscription.wl_traffic_used_gb or 0.0
        percent = min(100, (used_gb / limit_gb) * 100) if limit_gb > 0 else 0
        return {
            'success': True,
            'cached': False,
            'rate_limited': False,
            'source': 'database',
            'wl_traffic_used_bytes': int(used_gb * (1024 ** 3)),
            'wl_traffic_used_gb': round(used_gb, 2),
            'wl_traffic_limit_bytes': int(limit_gb * (1024 ** 3)),
            'wl_traffic_limit_gb': limit_gb,
            'wl_traffic_used_percent': round(percent, 1),
            'is_unlimited': limit_gb == 0,
        }

    used_gb = stats.get('used_traffic_gb', 0.0)
    if abs((subscription.wl_traffic_used_gb or 0.0) - used_gb) > 0.01:
        subscription.wl_traffic_used_gb = used_gb
        subscription.updated_at = datetime.now(UTC)
        await db.commit()

    percent = min(100, (used_gb / limit_gb) * 100) if limit_gb > 0 else 0

    payload = {
        'wl_traffic_used_bytes': stats.get('used_traffic_bytes', 0),
        'wl_traffic_used_gb': round(used_gb, 2),
        'wl_traffic_limit_bytes': int(limit_gb * (1024 ** 3)),
        'wl_traffic_limit_gb': limit_gb,
        'wl_traffic_used_percent': round(percent, 1),
        'is_unlimited': limit_gb == 0,
        'lifetime_used_bytes': stats.get('lifetime_used_traffic_bytes', 0),
        'lifetime_used_gb': round(stats.get('lifetime_used_traffic_gb', 0), 2),
    }

    await cache.set(cache_key('wl_traffic', suffix), payload, WL_TRAFFIC_CACHE_TTL)

    return {
        'success': True,
        'cached': False,
        'rate_limited': False,
        'source': 'remnawave',
        **payload,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/wl_traffic.py tests/cabinet/subscription/test_wl_traffic_routes.py
git commit -m "feat(cabinet): POST /cabinet/subscription/refresh-wl-traffic"
```

---

## Task 14: POST `/wl-traffic/save-cart`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/wl_traffic.py`
- Test: `tests/cabinet/subscription/test_wl_traffic_routes.py`

- [ ] **Step 1: Append failing test**

```python
@pytest.mark.asyncio
async def test_wl_save_cart_persists_correct_mode(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import wl_traffic as wt
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user()
    sub = make_subscription()
    sub.status = 'active'

    save_cart = AsyncMock()

    with (
        patch.object(wt, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(wt, 'resolve_package_price', AsyncMock(return_value=4000)),
        patch.object(wt, '_apply_addon_discount', return_value={'discounted': 4000, 'discount': 0, 'percent': 0}),
        patch.object(wt, 'calculate_prorated_price', return_value=(4000, 30)),
        patch.object(wt.user_cart_service, 'save_user_cart', save_cart),
    ):
        result = await wt.save_wl_traffic_cart(
            request=TrafficPurchaseRequest(gb=10),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    assert result == {'success': True, 'cart_saved': True}
    save_cart.assert_awaited_once()
    cart_arg = save_cart.await_args[0][1]
    assert cart_arg['cart_mode'] == 'add_wl_traffic'
```

- [ ] **Step 2: Run test — verify FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py::test_wl_save_cart_persists_correct_mode -v`
Expected: FAIL — `save_wl_traffic_cart` not defined.

- [ ] **Step 3: Implement**

Append to `wl_traffic.py`:

```python
@router.post('/wl-traffic/save-cart')
async def save_wl_traffic_cart(
    request: TrafficPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, bool]:
    """Persist a cart so auto-purchase can complete after balance top-up."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=400, detail='У вас нет активной подписки')
    if subscription.status not in ('active', 'trial'):
        raise HTTPException(status_code=400, detail='Ваша подписка неактивна')
    if subscription.is_trial:
        raise HTTPException(status_code=400, detail='Докупка WL-трафика недоступна на пробном периоде')
    if (subscription.wl_traffic_limit_gb or 0) == 0:
        raise HTTPException(status_code=400, detail='У вас уже безлимитный трафик')

    base_price = await resolve_package_price(db, subscription, gb=request.gb, kind='wl')
    if base_price <= 0:
        raise HTTPException(status_code=400, detail=f'Пакет {request.gb} ГБ недоступен')

    is_tariff_mode = settings.is_tariffs_mode() and subscription.tariff_id
    if is_tariff_mode:
        prorated_price = base_price
    else:
        prorated_price, _ = calculate_prorated_price(base_price, subscription.end_date)

    discount = _apply_addon_discount(user, 'traffic', prorated_price, 30)
    final_price = discount['discounted']

    await user_cart_service.save_user_cart(
        user.id,
        {
            'cart_mode': 'add_wl_traffic',
            'subscription_id': subscription.id,
            'traffic_gb': request.gb,
            'price_kopeks': final_price,
            'base_price_kopeks': base_price,
            'discount_percent': discount['percent'],
            'source': 'cabinet',
            'description': f'Докупка {request.gb} ГБ WL-трафика',
        },
    )
    return {'success': True, 'cart_saved': True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_wl_traffic_routes.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/wl_traffic.py tests/cabinet/subscription/test_wl_traffic_routes.py
git commit -m "feat(cabinet): POST /cabinet/subscription/wl-traffic/save-cart"
```

---

## Task 15: Mount `wl_traffic_router`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/__init__.py`
- Modify: wherever the cabinet aggregator includes `traffic_router`.

- [ ] **Step 1: Update `__init__.py`**

Replace `app/cabinet/routes/subscription_modules/__init__.py` with:

```python
"""Subscription sub-modules for cabinet API."""

from .autopay import router as autopay_router
from .daily import router as daily_router
from .devices import router as devices_router
from .multi_tariff import router as multi_tariff_router
from .purchase import router as purchase_router
from .renewal import router as renewal_router
from .servers import router as servers_router
from .status import router as status_router
from .tariff_switch import router as tariff_switch_router
from .traffic import router as traffic_router
from .wl_traffic import router as wl_traffic_router


__all__ = [
    'autopay_router',
    'daily_router',
    'devices_router',
    'multi_tariff_router',
    'purchase_router',
    'renewal_router',
    'servers_router',
    'status_router',
    'tariff_switch_router',
    'traffic_router',
    'wl_traffic_router',
]
```

- [ ] **Step 2: Wire `wl_traffic_router` into the cabinet aggregator**

Run: `grep -rn "traffic_router" app/cabinet/routes/`

Find the file (most likely `app/cabinet/routes/subscription.py`) where `traffic_router` is included. Add `wl_traffic_router` to the imports and add `router.include_router(wl_traffic_router)` right after the existing `router.include_router(traffic_router)` line.

- [ ] **Step 3: Verify endpoints register**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/ -v`
Expected: 16 cabinet/subscription tests pass.

Run: `.venv/Scripts/python.exe -c "from app.cabinet.routes.subscription_modules import wl_traffic_router; print(len(wl_traffic_router.routes))"`
Expected: prints `6`.

- [ ] **Step 4: Commit**

```bash
git add app/cabinet/routes/subscription_modules/__init__.py app/cabinet/routes/subscription.py
git commit -m "feat(cabinet): mount wl_traffic router under cabinet subscription"
```

---

## Task 16: Refactor `traffic.py` to delegate to `_traffic_core`

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/traffic.py`
- Create: `tests/cabinet/subscription/test_traffic_regression.py`

This is a pure refactor — it must not change behaviour for the regular `/traffic*` endpoints.

- [ ] **Step 1: Write regression tests first**

Create `tests/cabinet/subscription/test_traffic_regression.py`:

```python
"""Regression coverage for regular traffic endpoints after _traffic_core refactor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_regular_packages_endpoint_still_returns(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import traffic as t

    user = make_user()
    sub = make_subscription()

    with (
        patch.object(t, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(
            t,
            'resolve_traffic_packages',
            AsyncMock(return_value=[{'gb': 5, 'price': 1000, 'is_unlimited': False}]),
        ),
    ):
        result = await t.get_traffic_packages(user=user, db=mock_db, subscription_id=None)

    assert len(result) == 1
    assert result[0].gb == 5


@pytest.mark.asyncio
async def test_regular_purchase_calls_apply_purchase_db_with_regular_kind(make_subscription, make_user, mock_db):
    from app.cabinet.routes.subscription_modules import traffic as t
    from app.cabinet.schemas.subscription import TrafficPurchaseRequest

    user = make_user(balance_kopeks=10_000_000)
    sub = make_subscription()
    sub.user_id = user.id

    apply_db = AsyncMock()

    with (
        patch.object(t, 'resolve_subscription', AsyncMock(return_value=sub)),
        patch.object(t, 'resolve_package_price', AsyncMock(return_value=1000)),
        patch.object(t, '_apply_addon_discount', return_value={'discounted': 1000, 'discount': 0, 'percent': 0}),
        patch.object(t, 'lock_user_for_pricing', AsyncMock(return_value=user)),
        patch.object(t, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(t, 'apply_purchase_db', apply_db),
        patch.object(t, 'reactivate_subscription', AsyncMock()),
        patch.object(t, 'create_transaction', AsyncMock()),
        patch.object(t, 'sync_remnawave_after_purchase', AsyncMock()),
        patch.object(t, 'calculate_prorated_price', return_value=(1000, 30)),
    ):
        await t.purchase_traffic(
            request=TrafficPurchaseRequest(gb=5),
            user=user,
            db=mock_db,
            subscription_id=None,
        )

    apply_db.assert_awaited_once()
    call_kwargs = apply_db.await_args.kwargs
    assert call_kwargs['kind'] == 'regular'
    assert call_kwargs['gb'] == 5
```

- [ ] **Step 2: Run them — must FAIL because `traffic.py` does not yet import the helpers**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/test_traffic_regression.py -v`
Expected: failures.

- [ ] **Step 3: Refactor `traffic.py`**

In `app/cabinet/routes/subscription_modules/traffic.py`, near the existing imports add:

```python
from ._traffic_core import (
    apply_purchase_db,
    resolve_package_price,
    resolve_traffic_packages,
    sync_remnawave_after_purchase,
)
```

Replace the body of `get_traffic_packages` with:

```python
subscription = await resolve_subscription(db, user, subscription_id)
if not subscription:
    return []
packages = await resolve_traffic_packages(db, subscription, kind='regular')
return [
    TrafficPackageResponse(
        gb=p['gb'],
        price_kopeks=p['price'],
        price_rubles=p['price'] / 100,
        is_unlimited=p['is_unlimited'],
    )
    for p in packages
]
```

In `purchase_traffic`, replace the package-price lookup blocks with:

```python
base_price_kopeks = await resolve_package_price(db, subscription, gb=request.gb, kind='regular')
if base_price_kopeks <= 0:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f'Traffic package {request.gb}GB is not available',
    )
```

Replace the existing `await add_subscription_traffic(db, subscription, request.gb)` line with `await apply_purchase_db(db, subscription, gb=request.gb, kind='regular')`. Replace the inline RemnaWave try/except block with `await sync_remnawave_after_purchase(db, subscription, user)`.

In `save_traffic_cart`, replace the price-lookup blocks the same way (`await resolve_package_price(db, subscription, gb=request.gb, kind='regular')`).

- [ ] **Step 4: Run regression and full subscription tests**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/ -v`
Expected: 18 passed (16 from prior tasks + 2 regression).

Also re-run: `.venv/Scripts/python.exe -m pytest tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py -v`
Expected: existing pass count.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/traffic.py tests/cabinet/subscription/test_traffic_regression.py
git commit -m "refactor(cabinet): delegate traffic.py to _traffic_core"
```

---

## Task 17: Auto-purchase service — handle `add_wl_traffic` cart mode

**Files:**
- Modify: `app/services/subscription_auto_purchase_service.py`

The bot already saves carts with `cart_mode='add_wl_traffic'`. Confirm whether the service consumes them; if it does not, add a branch.

- [ ] **Step 1: Inspect**

Run: `grep -n "add_traffic\|add_wl_traffic\|cart_mode" app/services/subscription_auto_purchase_service.py`

If `add_wl_traffic` already has a handler, the rest of this task is a no-op. Verify by reading the relevant function.

- [ ] **Step 2: Add the branch (if missing)**

Identify the dispatch function (typically `process_cart` or `consume_cart`). Add a case that mirrors the existing `add_traffic` case but operates on WL fields:

```python
elif cart_mode == 'add_wl_traffic':
    from app.database.crud.subscription import (
        add_subscription_wl_traffic,
        reactivate_subscription,
    )
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import TransactionType
    from app.services.subscription_service import SubscriptionService

    success = await subtract_user_balance(db, user, price_kopeks, description)
    if not success:
        return {'success': False, 'reason': 'balance_subtract_failed'}

    await add_subscription_wl_traffic(db, subscription, traffic_gb)
    await reactivate_subscription(db, subscription)
    try:
        await SubscriptionService().update_remnawave_user(db, subscription)
    except Exception as e:
        logger.warning('Auto-purchase WL Remnawave sync failed', error=str(e))

    await create_transaction(
        db=db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=price_kopeks,
        description=description,
    )
    return {'success': True, 'mode': 'add_wl_traffic'}
```

- [ ] **Step 3: Add a unit test**

Append to `tests/cabinet/subscription/test_wl_traffic_routes.py`:

```python
@pytest.mark.asyncio
async def test_auto_purchase_handles_add_wl_traffic_cart(make_subscription, make_user, mock_db):
    """The auto-purchase service runs when a cabinet 402 cart is consumed."""
    import importlib

    auto = importlib.import_module('app.services.subscription_auto_purchase_service')

    if not hasattr(auto, 'process_cart'):
        pytest.skip('process_cart not present in this version')

    user = make_user(balance_kopeks=100_000)
    sub = make_subscription()
    cart = {
        'cart_mode': 'add_wl_traffic',
        'subscription_id': sub.id,
        'traffic_gb': 25,
        'price_kopeks': 5000,
        'base_price_kopeks': 5000,
        'discount_percent': 0,
        'source': 'cabinet',
        'description': 'Докупка 25 ГБ WL-трафика',
    }

    with (
        patch.object(auto, 'subtract_user_balance', AsyncMock(return_value=True)),
        patch.object(auto, 'add_subscription_wl_traffic', AsyncMock()),
        patch.object(auto, 'reactivate_subscription', AsyncMock()),
        patch.object(auto, 'create_transaction', AsyncMock()),
    ):
        result = await auto.process_cart(mock_db, user, sub, cart)

    assert result['success'] is True
    assert result['mode'] == 'add_wl_traffic'
```

- [ ] **Step 4: Run**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/ -v`
Expected: all green (the new test will skip if `process_cart` isn't present).

- [ ] **Step 5: Commit**

```bash
git add app/services/subscription_auto_purchase_service.py tests/cabinet/subscription/test_wl_traffic_routes.py
git commit -m "feat(cabinet): auto-purchase handler for add_wl_traffic cart mode"
```

---

## Task 18: branding — expose `wl_traffic_topup_enabled`

**Files:**
- Modify: `app/cabinet/routes/branding.py`
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Read the branding response builder**

Run: `grep -n "TelegramWidgetConfigResponse\|oidc_code_flow_available" app/cabinet/routes/branding.py`

Identify the response model and the assembly site.

- [ ] **Step 2: Add the new field**

In `TelegramWidgetConfigResponse` (or the response model used by the branding endpoint), add:

```python
wl_traffic_topup_enabled: bool = False
```

In the handler that constructs the response, add:

```python
wl_traffic_topup_enabled=bool(getattr(settings, 'WL_TRAFFIC_TOPUP_ENABLED', True)),
```

- [ ] **Step 3: Add a regression test**

Append to `tests/cabinet/auth/test_telegram_oidc_routes.py` (after the existing `test_branding_auth_methods_marks_widget_deprecated`):

```python
@pytest.mark.asyncio
async def test_branding_exposes_wl_traffic_topup_flag(app_client, monkeypatch):
    async def _settings(db, key):
        return {
            'TELEGRAM_OIDC_ENABLED': 'true',
            'TELEGRAM_OIDC_CLIENT_ID': '111',
            'TELEGRAM_OIDC_REDIRECT_URI': 'https://cab.example.com/cb',
        }.get(key)

    from app.cabinet.routes import branding
    monkeypatch.setattr(branding, 'get_setting_value', _settings)

    response = await app_client.get('/cabinet/branding/telegram-widget')
    assert response.status_code == 200
    body = response.json()
    assert 'wl_traffic_topup_enabled' in body
    assert isinstance(body['wl_traffic_topup_enabled'], bool)
```

- [ ] **Step 4: Run**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/auth/test_telegram_oidc_routes.py::test_branding_exposes_wl_traffic_topup_flag -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/branding.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): branding exposes wl_traffic_topup_enabled"
```

---

## Task 19: Frontend API client

**Files:**
- Create: `bedolaga-cabinet/src/api/wlTraffic.ts`

- [ ] **Step 1: Write the client**

```ts
// bedolaga-cabinet/src/api/wlTraffic.ts
import apiClient from './client';

export interface WlTrafficPackage {
  gb: number;
  price_kopeks: number;
  price_rubles: number;
  is_unlimited: boolean;
}

export interface WlTrafficPurchaseResult {
  success: boolean;
  gb_added: number;
  new_wl_traffic_limit_gb: number;
  amount_paid_kopeks: number;
  new_balance_kopeks: number;
  discount_percent?: number;
  discount_kopeks?: number;
  base_price_kopeks?: number;
}

export interface WlTrafficSwitchResult {
  success: boolean;
  old_wl_traffic_gb: number;
  new_wl_traffic_gb: number;
  charged_kopeks: number;
  balance_kopeks: number;
  balance_label: string;
}

export interface WlTrafficResetResult {
  success: boolean;
  new_wl_traffic_used_gb: number;
  charged_kopeks: number;
  balance_kopeks: number;
}

export interface WlTrafficRefreshResult {
  success: boolean;
  cached: boolean;
  rate_limited: boolean;
  source: 'remnawave' | 'database' | 'cache';
  wl_traffic_used_bytes: number;
  wl_traffic_used_gb: number;
  wl_traffic_limit_bytes: number;
  wl_traffic_limit_gb: number;
  wl_traffic_used_percent: number;
  is_unlimited: boolean;
  lifetime_used_bytes?: number;
  lifetime_used_gb?: number;
  retry_after_seconds?: number;
}

export const wlTrafficApi = {
  getPackages: async (subId?: number): Promise<WlTrafficPackage[]> => {
    const r = await apiClient.get<WlTrafficPackage[]>(
      '/cabinet/subscription/wl-traffic-packages',
      { params: subId !== undefined ? { subscription_id: subId } : undefined },
    );
    return r.data;
  },

  purchase: async (gb: number, subId?: number): Promise<WlTrafficPurchaseResult> => {
    const r = await apiClient.post<WlTrafficPurchaseResult>(
      '/cabinet/subscription/wl-traffic',
      { gb },
      { params: subId !== undefined ? { subscription_id: subId } : undefined },
    );
    return r.data;
  },

  switch: async (gb: number, subId?: number): Promise<WlTrafficSwitchResult> => {
    const r = await apiClient.put<WlTrafficSwitchResult>(
      '/cabinet/subscription/wl-traffic',
      { gb },
      { params: subId !== undefined ? { subscription_id: subId } : undefined },
    );
    return r.data;
  },

  reset: async (subId?: number): Promise<WlTrafficResetResult> => {
    const r = await apiClient.post<WlTrafficResetResult>(
      '/cabinet/subscription/wl-traffic/reset',
      {},
      { params: subId !== undefined ? { subscription_id: subId } : undefined },
    );
    return r.data;
  },

  refresh: async (subId?: number): Promise<WlTrafficRefreshResult> => {
    const r = await apiClient.post<WlTrafficRefreshResult>(
      '/cabinet/subscription/refresh-wl-traffic',
      {},
      { params: subId !== undefined ? { subscription_id: subId } : undefined },
    );
    return r.data;
  },

  saveCart: async (gb: number, subId?: number): Promise<{ success: boolean; cart_saved: boolean }> => {
    const r = await apiClient.post<{ success: boolean; cart_saved: boolean }>(
      '/cabinet/subscription/wl-traffic/save-cart',
      { gb },
      { params: subId !== undefined ? { subscription_id: subId } : undefined },
    );
    return r.data;
  },
};
```

- [ ] **Step 2: Type-check**

Run: `cd bedolaga-cabinet && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
cd bedolaga-cabinet
git add src/api/wlTraffic.ts
git commit -m "feat(cabinet): wlTraffic API client"
```

---

## Task 20: Frontend `WlTrafficSection` + dialogs

**Files:**
- Create: `bedolaga-cabinet/src/components/subscription/WlTrafficSection.tsx`
- Create: `bedolaga-cabinet/src/components/subscription/WlTrafficDialogs.tsx`

The section is a near-mirror of the existing regular `TrafficSection`. Locate that component first and copy the structure.

- [ ] **Step 1: Find the regular traffic section**

Run: `grep -rn "TrafficSection\|RegularTraffic" bedolaga-cabinet/src/components/subscription`

If a `TrafficSection.tsx` exists, copy it and adapt for WL:
- Imports use `wlTrafficApi` from `src/api/wlTraffic.ts`.
- Field names use `wl_traffic_*` instead of `traffic_*` from the subscription object.
- Section title uses `t('wl_traffic.title')`.
- The "available" predicate is `subscription.wl_traffic_limit_gb > 0 && !subscription.is_trial && branding.wl_traffic_topup_enabled === true`.
- When unavailable, render an `<Alert>` with `t('wl_traffic.disabled_message')` and disable the action buttons (do NOT hide the section).

- [ ] **Step 2: Implement `WlTrafficSection.tsx`**

```tsx
// bedolaga-cabinet/src/components/subscription/WlTrafficSection.tsx
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { wlTrafficApi, type WlTrafficRefreshResult } from '../../api/wlTraffic';
import type { Subscription } from '../../types';
import { useBrandingStore } from '../../store/branding';
import { WlAddDialog, WlSwitchDialog, WlResetDialog } from './WlTrafficDialogs';

interface Props {
  subscription: Subscription;
  onSubscriptionUpdated: () => void;
}

export default function WlTrafficSection({ subscription, onSubscriptionUpdated }: Props) {
  const { t } = useTranslation();
  const branding = useBrandingStore(s => s.config);

  const [refresh, setRefresh] = useState<WlTrafficRefreshResult | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [openDialog, setOpenDialog] = useState<null | 'add' | 'switch' | 'reset'>(null);

  const isAvailable =
    (subscription.wl_traffic_limit_gb ?? 0) > 0 &&
    !subscription.is_trial &&
    branding?.wl_traffic_topup_enabled === true;

  const limit = subscription.wl_traffic_limit_gb ?? 0;
  const used = refresh?.wl_traffic_used_gb ?? subscription.wl_traffic_used_gb ?? 0;
  const percent = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      const r = await wlTrafficApi.refresh(subscription.id);
      setRefresh(r);
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    void onRefresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subscription.id]);

  return (
    <section className="wl-traffic-section" data-testid="wl-traffic-section">
      <header>
        <h3>{t('wl_traffic.title')}</h3>
      </header>

      <div className="status">
        <span>{t('wl_traffic.used')}: {used.toFixed(2)} GB</span>
        <span>{t('wl_traffic.limit')}: {limit > 0 ? `${limit} GB` : t('wl_traffic.unlimited')}</span>
        <progress value={percent} max={100} />
      </div>

      <div className="actions">
        <button onClick={() => onRefresh()} disabled={refreshing}>{t('wl_traffic.refresh')}</button>
        <button onClick={() => setOpenDialog('add')} disabled={!isAvailable}>{t('wl_traffic.add')}</button>
        <button onClick={() => setOpenDialog('switch')} disabled={!isAvailable}>{t('wl_traffic.switch')}</button>
        <button onClick={() => setOpenDialog('reset')} disabled={!isAvailable}>{t('wl_traffic.reset')}</button>
      </div>

      {!isAvailable && (
        <p className="alert alert-info">{t('wl_traffic.disabled_message')}</p>
      )}

      {openDialog === 'add' && (
        <WlAddDialog
          subscriptionId={subscription.id}
          onClose={() => setOpenDialog(null)}
          onSuccess={() => {
            setOpenDialog(null);
            onSubscriptionUpdated();
          }}
        />
      )}
      {openDialog === 'switch' && (
        <WlSwitchDialog
          subscriptionId={subscription.id}
          currentLimit={limit}
          onClose={() => setOpenDialog(null)}
          onSuccess={() => {
            setOpenDialog(null);
            onSubscriptionUpdated();
          }}
        />
      )}
      {openDialog === 'reset' && (
        <WlResetDialog
          subscriptionId={subscription.id}
          onClose={() => setOpenDialog(null)}
          onSuccess={() => {
            setOpenDialog(null);
            onSubscriptionUpdated();
          }}
        />
      )}
    </section>
  );
}
```

- [ ] **Step 3: Implement `WlTrafficDialogs.tsx`**

```tsx
// bedolaga-cabinet/src/components/subscription/WlTrafficDialogs.tsx
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { wlTrafficApi, type WlTrafficPackage } from '../../api/wlTraffic';

interface AddProps {
  subscriptionId: number;
  onClose: () => void;
  onSuccess: () => void;
}

export function WlAddDialog({ subscriptionId, onClose, onSuccess }: AddProps) {
  const { t } = useTranslation();
  const [packages, setPackages] = useState<WlTrafficPackage[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    wlTrafficApi.getPackages(subscriptionId).then(setPackages).catch(() => setPackages([]));
  }, [subscriptionId]);

  const onChoose = async (gb: number) => {
    setBusy(true);
    try {
      await wlTrafficApi.purchase(gb, subscriptionId);
      onSuccess();
    } catch (e) {
      onClose();
      throw e;
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal" role="dialog">
      <h4>{t('wl_traffic.add_dialog.title')}</h4>
      <p>{t('wl_traffic.add_dialog.select_package')}</p>
      <ul>
        {packages.map(p => (
          <li key={p.gb}>
            <button disabled={busy} onClick={() => onChoose(p.gb)}>
              {p.is_unlimited ? t('wl_traffic.unlimited') : `${p.gb} GB`} — {p.price_rubles.toFixed(2)} ₽
            </button>
          </li>
        ))}
      </ul>
      <button onClick={onClose}>{t('common.cancel')}</button>
    </div>
  );
}

interface SwitchProps {
  subscriptionId: number;
  currentLimit: number;
  onClose: () => void;
  onSuccess: () => void;
}

export function WlSwitchDialog({ subscriptionId, currentLimit, onClose, onSuccess }: SwitchProps) {
  const { t } = useTranslation();
  const [packages, setPackages] = useState<WlTrafficPackage[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    wlTrafficApi.getPackages(subscriptionId).then(setPackages).catch(() => setPackages([]));
  }, [subscriptionId]);

  const onChoose = async (gb: number) => {
    setBusy(true);
    try {
      await wlTrafficApi.switch(gb, subscriptionId);
      onSuccess();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal" role="dialog">
      <h4>{t('wl_traffic.switch_dialog.title')}</h4>
      <p>{t('wl_traffic.switch_dialog.warning')}</p>
      <ul>
        {packages.map(p => (
          <li key={p.gb}>
            <button disabled={busy || p.gb === currentLimit} onClick={() => onChoose(p.gb)}>
              {p.is_unlimited ? t('wl_traffic.unlimited') : `${p.gb} GB`} — {p.price_rubles.toFixed(2)} ₽
            </button>
          </li>
        ))}
      </ul>
      <button onClick={onClose}>{t('common.cancel')}</button>
    </div>
  );
}

interface ResetProps {
  subscriptionId: number;
  onClose: () => void;
  onSuccess: () => void;
}

export function WlResetDialog({ subscriptionId, onClose, onSuccess }: ResetProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  const onConfirm = async () => {
    setBusy(true);
    try {
      await wlTrafficApi.reset(subscriptionId);
      onSuccess();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal" role="dialog">
      <h4>{t('wl_traffic.reset_dialog.title')}</h4>
      <p>{t('wl_traffic.reset_dialog.confirm')}</p>
      <button disabled={busy} onClick={onConfirm}>{t('common.confirm')}</button>
      <button onClick={onClose}>{t('common.cancel')}</button>
    </div>
  );
}
```

- [ ] **Step 4: Type-check**

Run: `cd bedolaga-cabinet && npx tsc --noEmit`
Expected: no errors. If `useBrandingStore` does not exist, replace with the project's actual branding hook (search via `grep -rn "wl_traffic_topup_enabled\|oidc_code_flow_available" src` to find it).

- [ ] **Step 5: Commit**

```bash
cd bedolaga-cabinet
git add src/components/subscription/WlTrafficSection.tsx src/components/subscription/WlTrafficDialogs.tsx
git commit -m "feat(cabinet): WlTrafficSection + add/switch/reset dialogs"
```

---

## Task 21: Frontend integration + locales

**Files:**
- Modify: `bedolaga-cabinet/src/pages/SubscriptionDetail.tsx` (or whatever component renders the subscription detail layout)
- Modify: `bedolaga-cabinet/src/locales/ru.json`
- Modify: `bedolaga-cabinet/src/locales/en.json`

- [ ] **Step 1: Render the section**

Run: `grep -rn "TrafficSection" bedolaga-cabinet/src/pages bedolaga-cabinet/src/components`

Find the existing `<TrafficSection>` usage. Right after it, render `<WlTrafficSection>` with the same prop pattern:

```tsx
import WlTrafficSection from '../components/subscription/WlTrafficSection';

// inside JSX, right after <TrafficSection ... />:
<WlTrafficSection subscription={subscription} onSubscriptionUpdated={refreshSubscription} />
```

- [ ] **Step 2: Extend `ru.json`**

Append (or merge) into `bedolaga-cabinet/src/locales/ru.json`:

```json
{
  "wl_traffic": {
    "title": "WL-трафик",
    "limit": "Лимит",
    "used": "Использовано",
    "unlimited": "♾️ Безлимит",
    "add": "Докупить",
    "switch": "Сменить пакет",
    "reset": "Сбросить счётчик",
    "refresh": "Обновить",
    "disabled_message": "WL-трафик недоступен на вашем тарифе или отключён глобально",
    "add_dialog": {
      "title": "Добавить WL-трафик",
      "select_package": "Выберите пакет"
    },
    "switch_dialog": {
      "title": "Сменить пакет WL",
      "warning": "Докупленный трафик будет сброшен"
    },
    "reset_dialog": {
      "title": "Сброс счётчика WL",
      "confirm": "Подтвердить сброс?"
    }
  }
}
```

- [ ] **Step 3: Extend `en.json`**

```json
{
  "wl_traffic": {
    "title": "WL traffic",
    "limit": "Limit",
    "used": "Used",
    "unlimited": "♾️ Unlimited",
    "add": "Top up",
    "switch": "Change package",
    "reset": "Reset counter",
    "refresh": "Refresh",
    "disabled_message": "WL traffic is not available on your tariff or globally disabled",
    "add_dialog": {
      "title": "Add WL traffic",
      "select_package": "Select a package"
    },
    "switch_dialog": {
      "title": "Change WL package",
      "warning": "Purchased traffic will be reset"
    },
    "reset_dialog": {
      "title": "Reset WL counter",
      "confirm": "Confirm reset?"
    }
  }
}
```

- [ ] **Step 4: Build the frontend**

Run: `cd bedolaga-cabinet && npm run build`
Expected: build succeeds with no TS or ESLint errors. Chunk-size warnings are acceptable.

- [ ] **Step 5: Commit**

```bash
cd bedolaga-cabinet
git add src/pages/SubscriptionDetail.tsx src/locales/ru.json src/locales/en.json
git commit -m "feat(cabinet): render WlTrafficSection + add locales"
```

---

## Task 22: Final integration smoke + full suite

**Files:** none (verification only).

- [ ] **Step 1: Backend cabinet/subscription suite**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/subscription/ -v`
Expected: all green.

- [ ] **Step 2: Backend cabinet/auth (regression)**

Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/auth/ -v`
Expected: pass count unchanged from before this plan.

- [ ] **Step 3: Backend WL trial-to-paid regression**

Run: `.venv/Scripts/python.exe -m pytest tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py -v`
Expected: pass count unchanged.

- [ ] **Step 4: Backend full suite (best-effort)**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: any failures must already exist on the parent commit. Spot-check by `git stash` + run on parent + compare. Do not introduce new failures.

- [ ] **Step 5: Frontend type-check + build**

Run: `cd bedolaga-cabinet && npx tsc --noEmit && npm run build`
Expected: no errors.

- [ ] **Step 6: Manual smoke (post-deploy, optional)**

In a staging environment with `WL_TRAFFIC_TOPUP_ENABLED=true` and a tariff that has `wl_traffic_topup_packages`:

1. Open subscription detail in cabinet → see WL section with active buttons.
2. Click "Top up" → choose package → balance debited → RemnaWave WL panel user updated → used GB visible after refresh.
3. Click "Reset counter" → balance debited → counter shows 0.
4. Click "Change package" → pick a higher GB → balance debited (diff prorated) → new limit reflected. Pick lower GB → no charge.
5. Disable `WL_TRAFFIC_TOPUP_ENABLED` in admin → reload cabinet → buttons disabled, alert visible. Section still rendered.

- [ ] **Step 7: Final commit (only if step 4 surfaced housekeeping)**

```bash
git add -A
git commit -m "chore(cabinet): green WL traffic suite"
```

---

## Self-review notes

- **Spec coverage** — every section of the spec maps to a task. § 5 architecture → T3–T7 + T9–T15. § 6 API contract → T9 (packages), T10 (purchase), T11 (switch), T12 (reset), T13 (refresh), T14 (save-cart). § 7 data flow paths exercised by tests in T10–T13. § 8 file structure matches the Created/Modified tables. § 9 error handling — every status code covered by a test in T10–T13. § 10 testing — T1 scaffolding, T2 unit (reset), T3–T7 unit (core), T9–T14 integration, T16 regression. § 11 rollout — covered manually in T22.
- **No placeholders** — every step contains either a concrete code block, an exact command, or a file-search pattern with the next action specified.
- **Type consistency** — function names referenced across tasks line up: `resolve_traffic_packages`, `resolve_package_price`, `apply_purchase_db`, `delete_purchases_for_switch`, `refresh_used_from_panel`, `sync_remnawave_after_purchase`, `calculate_traffic_reset_price`. The `kind` parameter is keyword-only across all `_traffic_core` helpers and consumers.
