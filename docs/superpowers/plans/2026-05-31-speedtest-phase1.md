# Speedtest фаза 1 (client-side ping) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать подписчику в кабинете кнопку «Проверить задержку» — измерить HTTP-RTT от его браузера до каждого узла, показать отсортированный список с лучшим.

**Architecture:** Backend отдаёт subscriber-gated список узлов с валидным `ping_host` (без сырого IP). React-страница меряет RTT client-side (медиана N, таймаут, параллель с капом). Узлы без `/ping`/TLS — «недоступен» (degrade-friendly). За env-флагом (дефолт OFF). Миграции нет.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async; React/TS (Vite) кабинет; pytest.

**Spec:** `docs/superpowers/specs/2026-05-31-speedtest-phase1-design.md`

**Run backend tests:** `.venv/Scripts/python.exe -m pytest <path> -v`
**Cabinet build check:** `cd bedolaga-cabinet && npm run build` (or `npx tsc --noEmit` for typecheck).

---

## File Structure

- `app/config.py` — 3 settings + getter (Task 1).
- `app/services/speedtest_settings_service.py` — JSON node→ping_host mapping + toggle (Task 1).
- `app/services/speedtest_service.py` — get_ping_targets + node cache (Task 2).
- `app/cabinet/routes/subscription_modules/speedtest.py` + `__init__.py` + `subscription.py` — endpoint + router reg (Task 3).
- `bedolaga-cabinet/src/utils/latency.ts` + test — pure median/sort (Task 4).
- `bedolaga-cabinet/src/api/speedtest.ts` + `pages/SpeedTest.tsx` + `App.tsx` + nav — frontend (Task 5).
- `docs/speedtest-node-setup.md` + `.env.example` (Task 6).

Tests: `tests/services/test_speedtest_service.py` (Task 2), `tests/cabinet/test_speedtest_route.py` (Task 3), cabinet `latency.test.ts` (Task 4).

---

## Task 1: config + settings service

**Files:**
- Modify: `app/config.py`
- Create: `app/services/speedtest_settings_service.py`, `tests/services/test_speedtest_settings.py`

**Context:** Mirror `app/services/freeze_settings_service.py` (JSON-on-disk, single config key). Config key `speedtest`.

- [ ] **Step 1: Add config to app/config.py**

In `class Settings`, near other feature flags, add:
```python
    SPEEDTEST_ENABLED: bool = False
    SPEEDTEST_SAMPLES: int = 5
    SPEEDTEST_PING_HOST_TEMPLATE: str = ''
```
Add a getter near other getters:
```python
    def get_speedtest_samples(self) -> int:
        try:
            return max(3, min(10, int(self.SPEEDTEST_SAMPLES)))
        except (TypeError, ValueError):
            return 5
```

- [ ] **Step 2: Write failing settings tests**

Create `tests/services/test_speedtest_settings.py`:
```python
import pytest

from app.services.speedtest_settings_service import SpeedtestSettingsService as SSS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(SSS, '_storage_path', tmp_path / 'speedtest_settings.json')
    monkeypatch.setattr(SSS, '_data', {})
    monkeypatch.setattr(SSS, '_loaded', False)
    yield


def test_defaults():
    assert SSS.is_enabled() is False
    assert SSS.get_host_mapping() == {}


def test_set_and_get_mapping():
    assert SSS.set_host_mapping({'uuid-1': 'node1.example.com'}) is True
    assert SSS.get_host_mapping() == {'uuid-1': 'node1.example.com'}


def test_mapping_rejects_non_dict():
    assert SSS.set_host_mapping('nope') is False


def test_resolve_host_strips_scheme_and_path():
    assert SSS.set_host_mapping({'u': 'https://node1.example.com/foo'}) is True
    # stored sanitized to bare hostname
    assert SSS.get_host_mapping()['u'] == 'node1.example.com'
```

- [ ] **Step 3: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_speedtest_settings.py -v` → ModuleNotFoundError.

- [ ] **Step 4: Implement**

Create `app/services/speedtest_settings_service.py`. Copy the `_ensure_dir`/`_load`/`_apply_defaults`/`_save`/`_get`/`_set_field`/`get_config` machinery verbatim from `freeze_settings_service.py` (storage `data/speedtest_settings.json`, key `'speedtest'`). `_DEFAULTS`:
```python
    _DEFAULTS = {'speedtest': {'enabled': False, 'host_mapping': {}}}
```
Add a hostname sanitizer (module-level) + methods:
```python
import re

_HOST_RE = re.compile(r'^[a-zA-Z0-9.-]+$')

def _sanitize_host(value: str) -> str | None:
    raw = (value or '').strip()
    if not raw:
        return None
    if '://' in raw:
        raw = raw.split('://', 1)[1]
    raw = raw.split('/', 1)[0].split(':', 1)[0].split('?', 1)[0].strip()
    if not raw or not _HOST_RE.match(raw):
        return None
    return raw
```
Classmethods:
- `is_enabled()` / `set_enabled(bool)`
- `get_host_mapping() -> dict` returns `cls._get().get('host_mapping', {})`
- `set_host_mapping(mapping) -> bool`: `if not isinstance(mapping, dict): return False`; build `cleaned = {k: _sanitize_host(v) for k,v in mapping.items()}` dropping entries where sanitize→None; `return cls._set_field('host_mapping', cleaned)`.

(`test_resolve_host_strips_scheme_and_path` expects the stored value sanitized to bare hostname.)

- [ ] **Step 5: Run → PASS (4 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_speedtest_settings.py -v`

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/services/speedtest_settings_service.py tests/services/test_speedtest_settings.py
git commit -m "feat(speedtest): config flags + settings service (host mapping)"
```

---

## Task 2: SpeedtestService (ping targets + node cache)

**Files:**
- Create: `app/services/speedtest_service.py`, `tests/services/test_speedtest_service.py`

**Context:** `RemnaWaveService().get_all_nodes()` (`app/services/remnawave_service.py:749`) returns `list[dict]` with keys `uuid, name, address, country_code, is_connected, is_node_online, users_online, ...` (returns `[]` on error). `SpeedtestSettingsService.get_host_mapping()` (Task 1). `settings.SPEEDTEST_PING_HOST_TEMPLATE`. Resolve `ping_host` per node: mapping[uuid] → else template.format(...) if template set → else skip. Cache nodes ~60s via `time.monotonic()` (allowed; not Date.now). Tests mock the node fetch.

- [ ] **Step 1: Write failing tests**

Create `tests/services/test_speedtest_service.py`:
```python
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.speedtest_service as ss
from app.services.speedtest_service import SpeedtestService


_NODES = [
    {'uuid': 'u1', 'name': 'NL-1', 'address': '1.2.3.4', 'country_code': 'NL',
     'is_node_online': True, 'users_online': 5},
    {'uuid': 'u2', 'name': 'DE-1', 'address': '5.6.7.8', 'country_code': 'DE',
     'is_node_online': False, 'users_online': 0},
]


@pytest.fixture
def service():
    svc = SpeedtestService()
    svc._remnawave = MagicMock()
    svc._remnawave.get_all_nodes = AsyncMock(return_value=[dict(n) for n in _NODES])
    svc._nodes_cache = None
    svc._nodes_cache_at = None
    return svc


@pytest.mark.asyncio
async def test_mapping_resolves_host(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com', 'u2': 'de1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    targets = await service.get_ping_targets()
    hosts = {t['ping_host'] for t in targets}
    assert hosts == {'nl1.example.com', 'de1.example.com'}
    assert all('address' not in t for t in targets)


@pytest.mark.asyncio
async def test_node_without_host_excluded(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    targets = await service.get_ping_targets()
    assert [t['ping_host'] for t in targets] == ['nl1.example.com']


@pytest.mark.asyncio
async def test_template_resolves_host(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping', classmethod(lambda cls: {}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '{node_name}.vpn.example.com', raising=False)
    targets = await service.get_ping_targets()
    hosts = sorted(t['ping_host'] for t in targets)
    assert hosts == ['DE-1.vpn.example.com', 'NL-1.vpn.example.com']


@pytest.mark.asyncio
async def test_cache_avoids_second_fetch(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    await service.get_ping_targets()
    await service.get_ping_targets()
    assert service._remnawave.get_all_nodes.await_count == 1


@pytest.mark.asyncio
async def test_target_shape(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    targets = await service.get_ping_targets()
    t = targets[0]
    assert set(t.keys()) == {'name', 'country_code', 'ping_host', 'is_online', 'users_online'}
```

- [ ] **Step 2: Run → FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_speedtest_service.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `app/services/speedtest_service.py`:
```python
from __future__ import annotations

import time

import structlog

from app.config import settings
from app.services.remnawave_service import RemnaWaveService
from app.services.speedtest_settings_service import SpeedtestSettingsService


logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 60


class SpeedtestService:
    def __init__(self) -> None:
        self._remnawave = RemnaWaveService()
        self._nodes_cache: list[dict] | None = None
        self._nodes_cache_at: float | None = None

    async def _get_nodes_cached(self) -> list[dict]:
        now = time.monotonic()
        if (
            self._nodes_cache is not None
            and self._nodes_cache_at is not None
            and (now - self._nodes_cache_at) < _CACHE_TTL_SECONDS
        ):
            return self._nodes_cache
        nodes = await self._remnawave.get_all_nodes()
        self._nodes_cache = nodes
        self._nodes_cache_at = now
        return nodes

    def _resolve_ping_host(self, node: dict, mapping: dict) -> str | None:
        host = mapping.get(node.get('uuid'))
        if host:
            return host
        template = settings.SPEEDTEST_PING_HOST_TEMPLATE
        if template:
            try:
                return template.format(
                    node_name=node.get('name', ''),
                    country_code=node.get('country_code', ''),
                )
            except Exception:
                return None
        return None

    async def get_ping_targets(self) -> list[dict]:
        nodes = await self._get_nodes_cached()
        mapping = SpeedtestSettingsService.get_host_mapping()
        targets = []
        for node in nodes:
            ping_host = self._resolve_ping_host(node, mapping)
            if not ping_host:
                continue
            targets.append({
                'name': node.get('name', ''),
                'country_code': node.get('country_code'),
                'ping_host': ping_host,
                'is_online': bool(node.get('is_node_online', node.get('is_connected', False))),
                'users_online': node.get('users_online', 0),
            })
        targets.sort(key=lambda t: ((t['country_code'] or ''), t['name']))
        return targets


speedtest_service = SpeedtestService()
```

- [ ] **Step 4: Run → PASS (5 tests)**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_speedtest_service.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/speedtest_service.py tests/services/test_speedtest_service.py
git commit -m "feat(speedtest): SpeedtestService ping-target resolver + node cache"
```

---

## Task 3: cabinet backend endpoint

**Files:**
- Create: `app/cabinet/routes/subscription_modules/speedtest.py`, `tests/cabinet/test_speedtest_route.py`
- Modify: `app/cabinet/routes/subscription_modules/__init__.py`, `app/cabinet/routes/subscription.py`

**Context:** Mirror `freeze.py` (already in repo): `router = APIRouter()`, deps `get_cabinet_db`/`get_current_cabinet_user` from `...dependencies`, `get_active_subscriptions_by_user_id` from `app.database.crud.subscription`. Routers registered in `subscription.py` (import block + `router.include_router(...)`), exported in `__init__.py`. `speedtest_service` singleton from Task 2.

- [ ] **Step 1: Implement the endpoint**

Create `app/cabinet/routes/subscription_modules/speedtest.py`:
```python
"""Speedtest: subscriber-gated node latency targets."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.models import User
from app.services.speedtest_service import speedtest_service

from ...dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get('/nodes-latency-targets')
async def nodes_latency_targets(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    if not settings.SPEEDTEST_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Speedtest disabled')
    subs = await get_active_subscriptions_by_user_id(db, user.id)
    if not subs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Subscription required')
    targets = await speedtest_service.get_ping_targets()
    return {'targets': targets, 'samples': settings.get_speedtest_samples()}
```
FIRST read `freeze.py` to confirm the exact dependency import paths; match them.

- [ ] **Step 2: Register the router**

- `__init__.py`: add `from .speedtest import router as speedtest_router` + `'speedtest_router'` in `__all__`.
- `subscription.py`: add `speedtest_router` to the `from .subscription_modules import (...)` block and `router.include_router(speedtest_router)` near `router.include_router(freeze_router)`.

- [ ] **Step 3: Write a route test**

Inspect `tests/cabinet/` for an existing route-test pattern (app fixture / dependency_overrides). If one exists, mirror it; cover: SPEEDTEST_ENABLED False → 404; enabled + no subscription → 403; enabled + subscription → 200 with `targets` + `samples`. Mock `speedtest_service.get_ping_targets` (AsyncMock) and `get_active_subscriptions_by_user_id`. If there is NO cabinet test infra, instead write a direct unit test in `tests/cabinet/test_speedtest_route.py` that calls the `nodes_latency_targets` coroutine directly with mocked `user`/`db`, monkeypatching `settings.SPEEDTEST_ENABLED` and the two awaited deps — and note the deviation in the report.

- [ ] **Step 4: Run + import check**

Run: `.venv/Scripts/python.exe -c "import app.cabinet.routes.subscription; print('OK')"`
Run: `.venv/Scripts/python.exe -m pytest tests/cabinet/test_speedtest_route.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/subscription_modules/speedtest.py app/cabinet/routes/subscription_modules/__init__.py app/cabinet/routes/subscription.py tests/cabinet/test_speedtest_route.py
git commit -m "feat(speedtest): subscriber-gated nodes-latency-targets endpoint"
```

---

## Task 4: frontend latency utils (pure + tested)

**Files:**
- Create: `bedolaga-cabinet/src/utils/latency.ts`, `bedolaga-cabinet/src/utils/latency.test.ts`

**Context:** Pure functions (no DOM) so they're unit-testable independent of the UI. Cabinet uses Vite. Check if `vitest` is configured (`bedolaga-cabinet/package.json`). If vitest exists, write+run the test; if not, still create `latency.ts` and note the test couldn't run.

- [ ] **Step 1: Check test harness**

Read `bedolaga-cabinet/package.json` — does it have `vitest` (devDep + `test` script)? Record the answer.

- [ ] **Step 2: Implement latency.ts**

Create `bedolaga-cabinet/src/utils/latency.ts`:
```typescript
export function median(values: number[]): number | null {
  const v = values.filter((x) => Number.isFinite(x)).slice().sort((a, b) => a - b);
  if (v.length === 0) return null;
  const mid = Math.floor(v.length / 2);
  return v.length % 2 === 0 ? (v[mid - 1] + v[mid]) / 2 : v[mid];
}

// Drop the first sample (TLS/connection setup), then take the median of the rest.
export function effectiveLatency(samples: number[]): number | null {
  if (samples.length === 0) return null;
  const rest = samples.length > 1 ? samples.slice(1) : samples;
  return median(rest);
}

export type LatencyTier = 'fast' | 'ok' | 'slow';

export function latencyTier(ms: number): LatencyTier {
  if (ms < 80) return 'fast';
  if (ms <= 150) return 'ok';
  return 'slow';
}

// Sort reachable targets ascending by latency; unreachable (null) go last.
export function sortByLatency<T extends { latency: number | null }>(items: T[]): T[] {
  return items.slice().sort((a, b) => {
    if (a.latency === null && b.latency === null) return 0;
    if (a.latency === null) return 1;
    if (b.latency === null) return -1;
    return a.latency - b.latency;
  });
}

// Name of the best (lowest-latency, reachable) target, or null.
export function bestTargetName<T extends { name: string; latency: number | null }>(items: T[]): string | null {
  const reachable = items.filter((i) => i.latency !== null) as Array<T & { latency: number }>;
  if (reachable.length === 0) return null;
  return reachable.reduce((best, cur) => (cur.latency < best.latency ? cur : best)).name;
}
```

- [ ] **Step 3: Implement test (if vitest available)**

Create `bedolaga-cabinet/src/utils/latency.test.ts`:
```typescript
import { describe, expect, it } from 'vitest';
import { median, effectiveLatency, latencyTier, sortByLatency, bestTargetName } from './latency';

describe('latency utils', () => {
  it('median odd/even', () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([4, 1, 2, 3])).toBe(2.5);
    expect(median([])).toBeNull();
  });
  it('effectiveLatency drops first sample', () => {
    expect(effectiveLatency([100, 20, 22, 24])).toBe(22);
    expect(effectiveLatency([50])).toBe(50);
    expect(effectiveLatency([])).toBeNull();
  });
  it('latencyTier thresholds', () => {
    expect(latencyTier(50)).toBe('fast');
    expect(latencyTier(120)).toBe('ok');
    expect(latencyTier(200)).toBe('slow');
  });
  it('sortByLatency puts unreachable last', () => {
    const r = sortByLatency([{ latency: 100 }, { latency: null }, { latency: 30 }]);
    expect(r.map((x) => x.latency)).toEqual([30, 100, null]);
  });
  it('bestTargetName ignores unreachable', () => {
    expect(bestTargetName([{ name: 'a', latency: null }, { name: 'b', latency: 40 }])).toBe('b');
    expect(bestTargetName([{ name: 'a', latency: null }])).toBeNull();
  });
});
```

- [ ] **Step 4: Run (if vitest) + typecheck**

If vitest: `cd bedolaga-cabinet && npx vitest run src/utils/latency.test.ts` → PASS.
Always: `cd bedolaga-cabinet && npx tsc --noEmit` → no new type errors from latency.ts.

- [ ] **Step 5: Commit**

```bash
git add bedolaga-cabinet/src/utils/latency.ts bedolaga-cabinet/src/utils/latency.test.ts
git commit -m "feat(speedtest): pure latency utils (median/sort/tier) + tests"
```

---

## Task 5: React SpeedTest page + api + route + nav

**Files:**
- Create: `bedolaga-cabinet/src/api/speedtest.ts`, `bedolaga-cabinet/src/pages/SpeedTest.tsx`
- Modify: `bedolaga-cabinet/src/App.tsx` (lazy import + protected route), `bedolaga-cabinet/src/components/layout/AppShell/DesktopSidebar.tsx` (nav) + sibling navs if they share the list

**Context:** API client `bedolaga-cabinet/src/api/client.ts` (`apiClient.get`); other api modules call paths like `/cabinet/...` (see `servers.ts`/`subscription.ts` — match the prefix). Route pattern in `App.tsx`: `<Route path="..." element={<ProtectedRoute><LazyPage><Page/></LazyPage></ProtectedRoute>} />`, lazy: `const Page = lazyWithRetry(() => import('./pages/Page'));`. Nav items in `DesktopSidebar.tsx` (~line 107) `navItems` array, conditional entries `...(flag ? [{path,label,icon}] : [])` (e.g. `wheelEnabled`, `hasContests`). Reuse latency utils (Task 4) + existing card components (inspect `Subscription.tsx`/`Connection.tsx`).

- [ ] **Step 1: api/speedtest.ts**

Create `bedolaga-cabinet/src/api/speedtest.ts`:
```typescript
import apiClient from './client';

export interface PingTarget {
  name: string;
  country_code: string | null;
  ping_host: string;
  is_online: boolean;
  users_online: number;
}

export interface LatencyTargetsResponse {
  targets: PingTarget[];
  samples: number;
}

export const speedtestApi = {
  getTargets: async (): Promise<LatencyTargetsResponse> => {
    const response = await apiClient.get('/cabinet/subscription/nodes-latency-targets');
    return response.data;
  },
};
```
Confirm the apiClient base path: check whether `apiClient` already prefixes `/cabinet` (then drop it here) or not — match exactly how `servers.ts`/`subscription.ts` build their URLs.

- [ ] **Step 2: pages/SpeedTest.tsx**

Create the page. Requirements:
- Button «Проверить задержку» (i18n key with RU fallback). On click: `speedtestApi.getTargets()`, then measure.
- Measurement per target: loop `samples` times: `const t0 = performance.now(); try { await fetch(\`https://${ping_host}/ping\`, { mode: 'no-cors', cache: 'no-store', signal: AbortSignal.timeout(3000) }); push(performance.now() - t0); } catch { /* sample failed */ }`. If ALL samples failed → `latency = null` (unreachable). Else `latency = effectiveLatency(samples)`.
  - `mode: 'no-cors'` is intentional: we time the round-trip, never read the body, so an opaque response is fine and CORS misconfig won't mask a reachable node. Add a code comment explaining this.
- Concurrency cap 4 (simple promise pool over targets).
- Render cards (reuse existing cabinet card components/styles): country flag (from `country_code`), name, latency ms + tier color (`latencyTier`), online/offline badge, ⚡ on `bestTargetName(results)`, «недоступен» when `latency===null`. Sort with `sortByLatency`.
- getTargets errors: 404 → «функция недоступна»; 403 → «нужна активная подписка»; other → generic error. Friendly, no crash.
- Keep the impure fetch loop in the component; reuse pure helpers from `utils/latency`.

- [ ] **Step 3: Register route in App.tsx**

Lazy import near others: `const SpeedTest = lazyWithRetry(() => import('./pages/SpeedTest'));`
Protected route near subscription routes:
```tsx
        <Route
          path="/speedtest"
          element={
            <ProtectedRoute>
              <LazyPage>
                <SpeedTest />
              </LazyPage>
            </ProtectedRoute>
          }
        />
```

- [ ] **Step 4: Add nav item (gated)**

In `DesktopSidebar.tsx` `navItems`, add a gated entry mirroring `wheelEnabled`/`hasContests` pattern. Derive `speedtestEnabled` the same way those flags are derived (find the hook/store/features source). If exposing `SPEEDTEST_ENABLED` to the frontend requires adding one field to an existing public-features/settings endpoint that already feeds `wheelEnabled` etc., do that minimal end-to-end addition. If that's larger than a one-field add, gate the nav on "always show when authenticated" and rely on the page's own 404/403 handling — note the choice in the report.
```typescript
    ...(speedtestEnabled ? [{ path: '/speedtest', label: t('nav.speedtest', 'Скорость'), icon: <ExistingIcon> }] : []),
```
Use an existing icon from that file's icon set. If `MobileBottomNav.tsx`/`FloatingDock.tsx`/`OrbitMenu.tsx` build from the same list (or a shared nav source), add there too.

- [ ] **Step 5: Typecheck + build**

Run: `cd bedolaga-cabinet && npx tsc --noEmit` → no new type errors.
Run: `cd bedolaga-cabinet && npm run build` → succeeds.

- [ ] **Step 6: Commit**

```bash
git add bedolaga-cabinet/src/api/speedtest.ts bedolaga-cabinet/src/pages/SpeedTest.tsx bedolaga-cabinet/src/App.tsx bedolaga-cabinet/src/components/layout/AppShell/
git commit -m "feat(speedtest): cabinet SpeedTest page + api + route + nav"
```

---

## Task 6: infra docs + env + final verify

**Files:**
- Create: `docs/speedtest-node-setup.md`
- Modify: `.env.example`

- [ ] **Step 1: Node-setup doc**

Create `docs/speedtest-node-setup.md`: operator requirement — each ping-able node needs HTTPS `/ping` → 204 with CORS for the cabinet origin, on a DNS name with valid TLS (Let's Encrypt). Include the nginx snippet from the spec, the `host_mapping` explanation (node uuid → ping_host) and `SPEEDTEST_PING_HOST_TEMPLATE` usage, and the note that nodes without this show «недоступен» (degrade-friendly).

- [ ] **Step 2: .env.example**

Add near other feature flags:
```
# Speedtest (cabinet client-side latency check). Requires HTTPS /ping + CORS + valid TLS on each node (see docs/speedtest-node-setup.md).
SPEEDTEST_ENABLED=false
SPEEDTEST_SAMPLES=5
# Optional ping-host template, e.g. {node_name}.vpn.example.com (used when no per-node mapping is set)
SPEEDTEST_PING_HOST_TEMPLATE=
```

- [ ] **Step 3: Backend regression**

Run: `.venv/Scripts/python.exe -m pytest tests/services/test_speedtest_settings.py tests/services/test_speedtest_service.py tests/cabinet/test_speedtest_route.py -v` → all PASS.
Run: `.venv/Scripts/python.exe -m pytest tests/services/ -q` → no NEW failures vs baseline (~29 pre-existing).
Run: `.venv/Scripts/python.exe -c "import app.cabinet.routes.subscription; import app.config; print(app.config.settings.SPEEDTEST_ENABLED)"` → `False`.

- [ ] **Step 4: Commit**

```bash
git add docs/speedtest-node-setup.md .env.example
git commit -m "docs(speedtest): node-setup infra requirement + env flags"
```

---

## Self-Review Checklist (controller runs before final review)

- [ ] No migration (no DB changes) — confirmed.
- [ ] Endpoint subscriber-gated + SPEEDTEST_ENABLED 404 gate; never leaks raw node IP/address.
- [ ] ping_host resolved via mapping → template → skip; node without valid host excluded.
- [ ] Node cache (60s) prevents per-click panel hammering; module singleton shared.
- [ ] Frontend: median-of-rest (drop first), timeout per sample, concurrency cap, unreachable handling, best marker, tier colors.
- [ ] latency utils pure + unit-tested; UI reuses existing cabinet components.
- [ ] Degrade-friendly: nodes without /ping/TLS show «недоступен», don't break others.
- [ ] All gated by SPEEDTEST_ENABLED (default OFF); nav item gated.
- [ ] Infra requirement documented (operator task, outside bot code).

## Out of plan scope (follow-ups)

- Admin-UI for host_mapping (v1 = JSON file / template).
- Phase 2: download/upload throughput test (test file on node).
- Per-tariff node lock flag in targets.
- Auto-select fastest server.
- Exposing SPEEDTEST_ENABLED via public-features endpoint if not already trivial (nav falls back to always-show-when-authenticated otherwise).
