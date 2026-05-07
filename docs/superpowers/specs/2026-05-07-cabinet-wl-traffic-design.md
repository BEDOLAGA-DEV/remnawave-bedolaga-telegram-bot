# Cabinet WL Traffic Parity — Design

**Date:** 2026-05-07
**Branch:** `claude/reverent-keller-85d0f8`
**Status:** Approved by user, awaiting plan

## 1. Problem

The Telegram bot exposes a full UX for Whitelabel (WL) traffic management — purchase additional GB, switch the WL package, reset the WL counter, view used/limit/purchased — implemented in `app/handlers/subscription/wl_traffic.py`. The cabinet web frontend has none of this. Users currently must context-switch to the bot to manage WL traffic, even though the cabinet already has parity for *regular* (`traffic_*`) traffic via `app/cabinet/routes/subscription_modules/traffic.py`.

The `Subscription` model carries two parallel sets of columns:

- `traffic_limit_gb`, `traffic_used_gb`, `purchased_traffic_gb`, `traffic_reset_at` — regular traffic (cabinet has full parity).
- `wl_traffic_limit_gb`, `wl_traffic_used_gb`, `wl_purchased_traffic_gb`, `wl_traffic_reset_at` — WL traffic (cabinet has zero coverage).

WL traffic corresponds to a separate Remnawave panel user with a `_wl` suffix (`SubscriptionService._build_wl_username`). Operations on WL must hit the WL panel user, not the main one.

## 2. Goals

- Bring the cabinet to full parity with the bot for WL traffic operations:
  - View current limit / used / purchased.
  - Add a WL traffic package (top-up).
  - Switch the WL package (upgrade or downgrade).
  - Reset the WL traffic counter.
  - Refresh used GB from the Remnawave WL panel user.
- Keep the existing regular-traffic endpoints working unchanged. The refactor must not regress `/cabinet/subscription/traffic*`.
- Share logic between regular and WL paths through an extracted `_traffic_core` helper module — DRY without ad-hoc duplication.
- Render the WL section in the cabinet UI even when WL is unavailable (buttons disabled + explanatory note) so the feature is discoverable.

## 3. Non-goals

- Touch the bot WL handler (`app/handlers/subscription/wl_traffic.py`) — it remains the source of truth for the Telegram client.
- Migrate database fields. `wl_*` columns already exist (migrations `0062_add_wl_traffic.py`, `0063_add_wl_tariff_traffic_fields.py`).
- Add new languages. Existing locales are extended; missing translations fall back to English.
- Add WL admin endpoints (analytics, settings) — already covered separately by `app/handlers/admin/wl_analytics.py`.
- Build a generic traffic engine that handles N tiers — only `regular` and `wl` are supported.

## 4. User-facing decisions captured during brainstorming

| # | Question | Decision |
|---|----------|----------|
| 1 | Cabinet operations needed? | Full parity: read + add + reset + switch. |
| 2 | Backend organisation? | Extract common base into `_traffic_core.py`; thin `traffic.py` and `wl_traffic.py` over it. |
| 3 | Endpoint URL shape? | `wl-` prefix mirroring existing `traffic-*` endpoints. |
| 4 | UI visibility? | Always render the section. Disable buttons + show alert when WL is unavailable. |

## 5. Architecture

```
┌─ Frontend (bedolaga-cabinet/src) ─────────────────────────────────┐
│  Subscription detail page                                          │
│    ├─ Traffic section (existing, regular)                          │
│    └─ WL Traffic section (NEW)                                     │
│         • Always visible                                           │
│         • Buttons disabled when wl_traffic_limit_gb=0 OR           │
│           settings.WL_TRAFFIC_TOPUP_ENABLED=false                  │
│         • Operations: view, add, reset, switch                     │
└──────────────┬─────────────────────────────────────────────────────┘
               ▼
┌─ FastAPI cabinet routes ──────────────────────────────────────────┐
│  app/cabinet/routes/subscription_modules/                          │
│    ├─ traffic.py        (existing — refactored to use core)        │
│    ├─ wl_traffic.py     (NEW)                                      │
│    └─ _traffic_core.py  (NEW — shared helpers)                     │
└──────────────┬─────────────────────────────────────────────────────┘
               ▼
┌─ Database / Service layer (existing) ─────────────────────────────┐
│  Subscription: wl_traffic_limit_gb, wl_traffic_used_gb,            │
│                wl_purchased_traffic_gb, wl_traffic_reset_at        │
│  CRUD: add_subscription_wl_traffic                                 │
│  SubscriptionService._build_wl_username, update_remnawave_user     │
│  RemnaWaveService.get_user_traffic_stats_by_uuid                   │
└────────────────────────────────────────────────────────────────────┘
```

`_traffic_core.py` is parameterised by `kind: Literal['regular', 'wl']`. Each helper uses the kind to pick the right field names and the right Remnawave panel user. Endpoint modules become thin FastAPI wrappers.

## 6. API contract (under `/cabinet/subscription/`)

All endpoints accept an optional `?subscription_id=<int>` query parameter (multi-tariff routing) and require a JWT.

### 6.1 `GET /wl-traffic-packages`

Return available WL packages.

**Response** (`list[TrafficPackageResponse]`):

```json
[
  {"gb": 10, "price_kopeks": 5000, "price_rubles": 50.0, "is_unlimited": false},
  {"gb": 0,  "price_kopeks": 100000, "price_rubles": 1000.0, "is_unlimited": true}
]
```

Logic:

1. Resolve subscription via `resolve_subscription`.
2. If trial, return `[]`.
3. If `WL_TRAFFIC_TOPUP_ENABLED=false`, return `[]`.
4. If tariff mode and `tariff.wl_traffic_topup_packages` is non-empty, use those.
5. Else fall back to global `settings.get_wl_traffic_packages()`.
6. If `wl_traffic_limit_gb == 0` (already unlimited), return `[]`.

### 6.2 `POST /wl-traffic`

Purchase additional WL GB.

**Request** (`TrafficPurchaseRequest`):

```json
{ "gb": 50 }
```

**Response (200):**

```json
{
  "success": true,
  "gb_added": 50,
  "new_wl_traffic_limit_gb": 100,
  "amount_paid_kopeks": 4900,
  "new_balance_kopeks": 12300,
  "discount_percent": 10,
  "discount_kopeks": 500,
  "base_price_kopeks": 5400
}
```

Server flow:

1. `restriction_subscription` check → 403.
2. `resolve_subscription` → 404 on miss.
3. Validate not trial, not unlimited, WL enabled, tariff allows, package valid.
4. `lock_user_for_pricing` to prevent TOCTOU on promo state.
5. Compute price: tariff package or global, with discount and prorating (classic only).
6. Insufficient balance: save cart `cart_mode='add_wl_traffic'` + 402.
7. `subtract_user_balance`, `add_subscription_wl_traffic` (creates `WlTrafficPurchase`, updates limit + purchased + reset_at), `reactivate_subscription`.
8. `SubscriptionService.update_remnawave_user(subscription)` for the WL panel user.
9. `create_transaction`, admin notify.

### 6.3 `PUT /wl-traffic`

Switch WL package (upgrade or downgrade).

**Request:** `{ "gb": <new_limit> }`. **Response (200):**

```json
{
  "success": true,
  "old_wl_traffic_gb": 50,
  "new_wl_traffic_gb": 100,
  "charged_kopeks": 1500,
  "balance_kopeks": 10000,
  "balance_label": "100.00 ₽"
}
```

Logic:

1. `current = subscription.wl_traffic_limit_gb`, `purchased = wl_purchased_traffic_gb`, `base = current - purchased`.
2. Use `settings.get_wl_traffic_price(base)` and `settings.get_wl_traffic_price(new_gb)` for diff (mirrors bot).
3. Apply discount and prorating; only charge on upgrade.
4. Insufficient balance: 402 (no cart save — switch is not auto-resumed).
5. `DELETE FROM WlTrafficPurchase WHERE subscription_id=X`, set `wl_traffic_limit_gb=new_gb`, `wl_purchased_traffic_gb=0`, `wl_traffic_reset_at=None`.
6. `update_remnawave_user`.

### 6.4 `POST /wl-traffic/reset`

Reset the WL traffic counter.

**Request:** empty body. **Response (200):**

```json
{
  "success": true,
  "new_wl_traffic_used_gb": 0,
  "charged_kopeks": 12000,
  "balance_kopeks": 8000
}
```

Logic:

1. Validate not trial, not unlimited, not topup-blocked.
2. `reset_price = _calculate_traffic_reset_price(subscription)` (modes: `period`, `traffic`, `traffic_with_purchased`).
3. Insufficient balance: 402 (no cart save).
4. `subtract_user_balance`, `subscription.wl_traffic_used_gb = 0`, `updated_at = now`.
5. Resolve `_wl` panel username via `SubscriptionService._build_wl_username` (primary then legacy fallback).
6. Best-effort `api.reset_user_traffic(wl_user.uuid)` — non-fatal on failure, log warning.
7. `create_transaction`.

### 6.5 `POST /refresh-wl-traffic`

Pull fresh used-bytes from the Remnawave WL panel user.

Rate limit: 1 / 60 s per `(user_id[, subscription_id])`. Cache TTL 60 s (mirrors regular `/refresh-traffic`).

**Response (200):**

```json
{
  "success": true,
  "cached": false,
  "source": "remnawave",
  "wl_traffic_used_bytes": 12345678,
  "wl_traffic_used_gb": 0.01,
  "wl_traffic_limit_bytes": 107374182400,
  "wl_traffic_limit_gb": 100,
  "wl_traffic_used_percent": 0.0,
  "is_unlimited": false,
  "lifetime_used_bytes": 5000000,
  "lifetime_used_gb": 0.005
}
```

When the rate limit hits and a cached value exists, return `cached=true, rate_limited=true` instead of 429.

When Remnawave is unavailable, return `source="database"` with the last persisted values.

### 6.6 `POST /wl-traffic/save-cart`

Mirror of `/traffic/save-cart`. Persists `cart_mode='add_wl_traffic'` so the existing auto-purchase service can complete the transaction after a balance top-up.

**Request:** `{ "gb": 50 }`. **Response:** `{ "success": true, "cart_saved": true }`.

## 7. Data flow

### 7.1 Add WL traffic (happy path)

Frontend POSTs to `/wl-traffic`. Backend resolves the subscription, validates, locks the user row, prices, charges balance, persists the WL purchase, reactivates the subscription, syncs the WL Remnawave user, records the transaction, notifies admins, returns the new state.

### 7.2 Insufficient balance

Backend returns 402 with `cart_saved=true`. The user is redirected to the balance top-up page. After the deposit, `subscription_auto_purchase_service` consumes the cart (`cart_mode='add_wl_traffic'`) and runs the same purchase logic.

### 7.3 Reset

Reset price comes from `_calculate_traffic_reset_price` with one of three configured modes. After the local DB update, the backend resolves the WL panel username (primary → legacy fallback) and calls `api.reset_user_traffic`. The Remnawave call is best-effort: if it fails, the local reset still stands and a warning is logged.

### 7.4 Switch package

Switching always wipes accumulated `WlTrafficPurchase` rows and resets `wl_purchased_traffic_gb` and `wl_traffic_reset_at`. Upgrade charges the diff; downgrade charges nothing and does not refund. Then `update_remnawave_user` runs.

### 7.5 Refresh

Rate-limited identically to regular refresh. The backend resolves the WL panel user, fetches `used_traffic_bytes`, persists the change if drift > 0.01 GB, caches the result, and returns the snapshot. On Remnawave failure, the database snapshot is returned (`source="database"`).

## 8. Files

### 8.1 Modified

| File | Change |
|------|--------|
| `app/cabinet/routes/subscription_modules/traffic.py` | Refactor to delegate to `_traffic_core` (regular path). No behaviour change. |
| `app/cabinet/routes/subscription_modules/__init__.py` | Register the new `wl_traffic` router. |
| `app/cabinet/schemas/subscription.py` | Add `WlTrafficStatusResponse`, `WlTrafficResetResponse`, `WlTrafficSwitchResponse`. Reuse `TrafficPackageResponse` and `TrafficPurchaseRequest`. |
| `app/services/subscription_auto_purchase_service.py` | Confirm or add a handler for `cart_mode='add_wl_traffic'`. Bot already saves carts with this mode; if no consumer exists, add one. |
| `app/cabinet/routes/branding.py` | Expose `wl_traffic_topup_enabled` in the branding payload so the frontend can compute `isWlAvailable`. |
| `bedolaga-cabinet/src/api/subscription.ts` | Add `wlTrafficApi` (`getPackages`, `purchase`, `switch`, `reset`, `refresh`, `saveCart`). |
| `bedolaga-cabinet/src/pages/SubscriptionDetail.tsx` (or equivalent) | Render `<WlTrafficSection>` after the existing traffic section. |
| `bedolaga-cabinet/src/locales/{ru,en}.json` | Add `wl_traffic.*` keys. |

### 8.2 New

| File | Purpose |
|------|---------|
| `app/cabinet/routes/subscription_modules/_traffic_core.py` | Shared helpers parameterised by `kind`. |
| `app/cabinet/routes/subscription_modules/wl_traffic.py` | FastAPI router with the six endpoints. |
| `bedolaga-cabinet/src/components/subscription/WlTrafficSection.tsx` | Mirror of the regular traffic section, hooked to WL endpoints. |
| `bedolaga-cabinet/src/components/subscription/WlTrafficDialogs.tsx` | Add / switch / reset modals. |
| `tests/cabinet/subscription/__init__.py` | Package marker. |
| `tests/cabinet/subscription/test_traffic_core.py` | Unit tests for `_traffic_core` (parameterised over `kind`). |
| `tests/cabinet/subscription/test_wl_traffic_routes.py` | Integration tests for the six WL endpoints. |
| `tests/cabinet/subscription/test_traffic_regression.py` | Regression coverage for existing regular endpoints after the refactor. |

### 8.3 Not touched

- `app/handlers/subscription/wl_traffic.py` (bot UX).
- `app/database/crud/subscription.py:add_subscription_wl_traffic`.
- `app/database/models.py:Subscription`.
- Migrations — `wl_*` columns already exist.

## 9. Error handling

### 9.1 Per endpoint

| Endpoint | Status | Trigger | Detail |
|----------|--------|---------|--------|
| `GET /wl-traffic-packages` | 200 | OK | `[...packages]` (empty when WL disabled or unlimited) |
| `POST /wl-traffic` | 400 | trial subscription | `Эта функция доступна только для платных подписок` |
| `POST /wl-traffic` | 400 | unlimited (`wl_traffic_limit_gb=0`) | `У вас уже безлимитный трафик` |
| `POST /wl-traffic` | 400 | `WL_TRAFFIC_TOPUP_ENABLED=false` | `Функция докупки WL-трафика отключена` |
| `POST /wl-traffic` | 400 | tariff has no WL packages (classic mode) | `Докупка WL-трафика недоступна на вашем тарифе` |
| `POST /wl-traffic` | 400 | invalid gb (not in packages) | `Пакет {gb} ГБ недоступен` |
| `POST /wl-traffic` | 400 | package price is 0 | `Цена для этого пакета не настроена` |
| `POST /wl-traffic` | 402 | insufficient balance | `{code: 'insufficient_funds', missing_amount, cart_saved: true, cart_mode: 'add_wl_traffic'}` |
| `POST /wl-traffic` | 403 | `restriction_subscription` set | `Subscription purchases are restricted` |
| `POST /wl-traffic` | 404 | no subscription | `No subscription found` |
| `POST /wl-traffic` | 500 | balance subtract failed | `Failed to charge balance` |
| `PUT /wl-traffic` | 400 | new gb equals current | `Already on this WL traffic package` |
| `PUT /wl-traffic` | 400 | new gb invalid | `Invalid WL traffic package` |
| `PUT /wl-traffic` | 402 | insufficient (upgrade only) | `Insufficient balance` (no cart save) |
| `POST /wl-traffic/reset` | 400 | trial / unlimited / topup blocked | as above |
| `POST /wl-traffic/reset` | 402 | insufficient balance | `Insufficient balance` (no cart save) |
| `POST /wl-traffic/reset` | 500 | balance subtract failed | `Failed to charge balance` |
| `POST /refresh-wl-traffic` | 429 | rate limited | `Rate limited`, `Retry-After: 60` |
| `POST /refresh-wl-traffic` | 200 | cache fallback under rate limit | `{cached: true, rate_limited: true, ...data}` |
| `POST /refresh-wl-traffic` | 500 | upstream Remnawave failed | `Failed to refresh WL traffic data` |
| All | 401 | missing/invalid JWT | per `get_current_cabinet_user` |

### 9.2 Edge cases

1. **WL panel user does not exist in Remnawave** — `update_remnawave_user` will create it; on hard failure enqueue via `remnawave_retry_queue`.
2. **Switch with no purchases** — `WlTrafficPurchase` is empty, `DELETE` no-op, `wl_purchased_traffic_gb` already 0.
3. **Reset price mode `traffic_with_purchased`** — base = `wl_traffic_limit_gb - wl_purchased_traffic_gb`. All three modes are tested.
4. **Concurrent purchase + refresh** — rate limit + `lock_user_for_pricing` prevent the dangerous race; eventual consistency for the refreshed used GB is acceptable.
5. **Multi-tariff mode** — `subscription_id` query param is honoured by `resolve_subscription`. Without it, the default subscription is used.
6. **Trial → Paid migration** — covered by the existing `tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py`. The refactor must not regress it.

### 9.3 Logging

Structured fields: `user_id`, `subscription_id`, `kind=wl`, `gb`, `price_kopeks`, `discount_percent`, `client_ip`. Never log balance values, promo group internals, or raw user input beyond what is already logged for regular traffic.

### 9.4 Rate limits

- `/wl-traffic-packages`: none (read-only).
- `POST /wl-traffic`, `PUT /wl-traffic`: none (protected by `lock_user_for_pricing` and balance checks).
- `POST /wl-traffic/reset`: none (price is the natural throttle).
- `POST /refresh-wl-traffic`: 1 / 60 s per user (per subscription in multi-tariff mode).

## 10. Testing

### 10.1 Unit (`tests/cabinet/subscription/test_traffic_core.py`)

| Test | Coverage |
|------|----------|
| `test_resolve_packages_regular_tariff_mode` | `kind='regular'` reads `tariff.traffic_topup_packages`. |
| `test_resolve_packages_wl_tariff_mode` | `kind='wl'` reads `tariff.wl_traffic_topup_packages`. |
| `test_resolve_packages_global_fallback_regular` | Classic mode reads `settings.get_traffic_topup_packages()`. |
| `test_resolve_packages_global_fallback_wl` | Classic mode reads `settings.get_wl_traffic_packages()`. |
| `test_calculate_reset_price_period_mode` | mode=period → `PERIOD_PRICES[30]`. |
| `test_calculate_reset_price_traffic_mode` | mode=traffic → `max(traffic_price, base_price)`. |
| `test_calculate_reset_price_traffic_with_purchased` | mode=traffic_with_purchased → base + purchased. |
| `test_apply_purchase_db_regular` | Updates `traffic_*` fields, creates `TrafficPurchase`. |
| `test_apply_purchase_db_wl` | Updates `wl_traffic_*` fields, creates `WlTrafficPurchase`. |
| `test_sync_remnawave_regular` | Calls `update_remnawave_user(main user)`. |
| `test_sync_remnawave_wl` | Resolves `_wl` username, calls the corresponding update. |

### 10.2 Integration (`tests/cabinet/subscription/test_wl_traffic_routes.py`)

Approximately 22 tests covering happy paths and every error branch from § 9.1, plus auth gating. Reuses the `app_client` fixture pattern from `tests/cabinet/auth/`.

### 10.3 Regression (`tests/cabinet/subscription/test_traffic_regression.py`)

- `test_regular_purchase_still_works`
- `test_regular_switch_still_works`
- `test_regular_refresh_still_works`
- `test_regular_packages_still_returns`

### 10.4 Manual smoke (post-deploy)

1. Subscription with WL packages → cabinet shows the WL section with active buttons.
2. Subscription without WL (or WL globally off) → section is visible, buttons disabled, alert shown.
3. Click "Add 50GB" → balance is debited, Remnawave WL user is updated, used GB refreshes correctly.
4. Click "Reset" → counter is 0, Remnawave reflects.
5. Click "Switch package" → upgrade charges the diff, downgrade does not charge, purchases are reset.

### 10.5 Coverage targets

- `_traffic_core.py`: ≥ 90 %.
- `wl_traffic.py` route handlers: ≥ 95 % (security/billing critical).

## 11. Migration / rollout

1. Land the refactor and the new endpoints behind no flag (`WL_TRAFFIC_TOPUP_ENABLED` already exists for global control).
2. Set `WL_TRAFFIC_TOPUP_ENABLED=true` in the environments that should expose WL.
3. Frontend release after backend is green — the new section uses 6 endpoints that must exist. Backwards-compatible: if the frontend is older, nothing breaks; if the backend is older, the frontend section stays disabled.
4. Monitor: track `cart_mode='add_wl_traffic'` cart count to confirm the auto-purchase path is consumed by users.

## 12. Out of scope

- Bot WL handler changes.
- Generic N-tier engine.
- New languages / translations beyond `ru` + `en`.
- WL admin analytics endpoints (already exist outside cabinet).
- Removing or merging the regular and WL field sets in the database.
