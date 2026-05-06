# Telegram Cabinet Auth Rewrite — OIDC migration

**Date:** 2026-05-06
**Branch:** `claude/reverent-keller-85d0f8`
**Status:** Approved by user, awaiting plan

## 1. Problem

Cabinet currently exposes three Telegram authentication paths:

1. `POST /auth/telegram` — Mini-App `initData` (HMAC-SHA256 with `WebAppData` secret). Stays.
2. `POST /auth/telegram/widget` — Legacy iframe Login Widget (hash-check using SHA-256 of bot token). Documentation is now archived; Telegram has deprecated this in favor of OIDC.
3. `POST /auth/telegram/oidc` — Popup OIDC flow returning `id_token` (validated via JWKS, RS256). Already partially implemented.

The Telegram Login docs at <https://core.telegram.org/bots/telegram-login> describe a modern OpenID Connect flow with two supported variants (popup `id_token` and Authorization Code + PKCE), nonce binding, and configuration through *BotFather → Bot Settings → Web Login* (Client ID + Client Secret).

Current gaps versus the new spec:

- No server-side Authorization Code + PKCE flow. `TELEGRAM_OIDC_CLIENT_SECRET` is defined in `app/config.py` but unused.
- No nonce validation. Replay protection is hash-only (`TokenReplayCache`).
- Legacy widget endpoint still active.
- `/auth/account/link/telegram` mirrors all three modes including widget.

## 2. Goals

- Make cabinet Telegram auth fully aligned with the modern OIDC spec.
- Support both **popup** (primary, fast UX) and **Authorization Code + PKCE** (fallback for environments where popups are blocked).
- Add **nonce** validation on top of the existing `TokenReplayCache` (defense in depth).
- Deprecate the legacy widget endpoint with a 1–2 release grace period (HTTP 410 + migration link).
- Synchronize linking endpoint (`/auth/account/link/telegram`) with the new shape: drop widget mode, add nonce, add code+PKCE flow.

## 3. Non-goals

- Add `phone` or `telegram:bot_access` scopes (kept at `openid profile`).
- Migrate Mini-App `initData` flow — it stays as-is.
- Touch the deep-link bot fallback (`/auth/telegram/deep-link/*`) which already covers the case where `oauth.telegram.org` is blocked.
- Remove widget code in this iteration. Endpoints return HTTP 410, code stays for 1–2 releases.
- Build a frontend cabinet (lives in a separate repo). We provide a static test page only.

## 4. User-facing decisions captured during brainstorming

| # | Question | Decision |
|---|----------|----------|
| 1 | Legacy widget endpoint? | Deprecate, return HTTP 410 + migration link. Remove after 1–2 releases. |
| 2 | OIDC flows to support? | Both popup (primary) and Authorization Code + PKCE (fallback). |
| 3 | Scopes? | `openid profile`. |
| 4 | Replay protection? | Nonce **plus** existing `TokenReplayCache`. |
| 5 | Frontend deliverables? | Backend + JS snippet + static HTML test page. |
| 6 | Linking endpoint? | Full sync — drop widget mode, add nonce, add code+PKCE branch. |

## 5. Architecture

```
┌─ Frontend (separate repo) ────────────────────────────┐
│  popup:  Telegram.Login.open({bot_id, nonce}) → id_token
│  code:   redirect → oauth.telegram.org/auth?... → /callback?code=...
└──────────────┬────────────────────────────────────────┘
               ▼
┌─ FastAPI cabinet routes (app/cabinet/routes/auth.py) ─┐
│  POST /auth/telegram/oidc          (popup id_token)   │
│  GET  /auth/telegram/oidc/init     (NEW — code flow)  │
│  POST /auth/telegram/oidc/callback (NEW — code flow)  │
│  POST /auth/telegram/widget        (DEPRECATED, 410)  │
│  POST /auth/telegram/link-widget   (DEPRECATED, 410)  │
└──────────────┬────────────────────────────────────────┘
               ▼
┌─ Auth core (app/cabinet/auth/telegram_auth.py) ───────┐
│  validate_telegram_oidc_token(id_token, client_id,    │
│                                expected_nonce=None)   │
│  exchange_authorization_code(code, code_verifier,     │
│                               redirect_uri)  NEW      │
│  + JWKS cache (existing)                              │
└──────────────┬────────────────────────────────────────┘
               ▼
┌─ Caches (app/utils/cache.py) ─────────────────────────┐
│  OAuthStateCache  (state + PKCE verifier + nonce)     │
│  TokenReplayCache (id_token hash)                     │
└───────────────────────────────────────────────────────┘
```

**Key principle:** a single source of truth for claims validation — `validate_telegram_oidc_token()`. Both popup and code-flow paths feed into it after obtaining an `id_token`.

## 6. API contract

### 6.1 NEW: `POST /auth/telegram/oidc/init`

Initialize the Authorization Code + PKCE flow. Used by both login (no JWT) and linking (JWT required).

**Request:**

```json
{
  "mode": "login",
  "campaign_slug": "string?",
  "referral_code": "string?"
}
```

`mode` is one of `"login"` (no JWT) or `"link"` (JWT required).

**Response:**

```json
{
  "authorize_url": "https://oauth.telegram.org/auth?client_id=...&response_type=code&scope=openid+profile&redirect_uri=...&state=<S>&code_challenge=<C>&code_challenge_method=S256&nonce=<N>",
  "state": "<S>",
  "expires_in": 600
}
```

**Server-side actions:**

- Generate `state` (64 hex), `code_verifier` (43–128 url-safe chars), `code_challenge = base64url(sha256(verifier))` (no padding), `nonce` (32 hex).
- Store under `oauth_state:<state>` in Redis: `{ provider: "telegram", flow: "login|link", code_verifier, nonce, campaign_slug?, referral_code?, user_id? }` with TTL 600 s.
- Read `redirect_uri` from setting `TELEGRAM_OIDC_REDIRECT_URI` (DB override → env).
- Rate limit: 10/min/IP (login) or 10/min/user (link).

### 6.2 NEW: `POST /auth/telegram/oidc/callback`

Exchange the authorization code for an `id_token` and complete login or linking.

**Request:**

```json
{ "code": "string", "state": "string" }
```

**Response (login):** `AuthResponse` (existing schema).
**Response (link):** `LinkCallbackResponse` (existing schema, including possible `merge_required`).

**Server-side flow:**

1. Atomically consume `state` from `OAuthStateCache` (GETDEL — one-time use).
2. Pull `code_verifier`, `nonce`, `flow`, `campaign_slug`, `referral_code`, `user_id?` from state data.
3. POST `https://oauth.telegram.org/token` with `Basic` auth (`client_id:client_secret`) and form body:
   ```
   grant_type=authorization_code
   code=<code>
   redirect_uri=<TELEGRAM_OIDC_REDIRECT_URI>
   client_id=<client_id>
   code_verifier=<verifier>
   ```
   Timeout 10 s. Use existing `httpx.AsyncClient` with `settings.PROXY_URL` if set.
4. Extract `id_token` → `validate_telegram_oidc_token(id_token, client_id, expected_nonce=nonce)`.
5. `TokenReplayCache.is_token_replayed(sha256(id_token), ttl=min(exp-now, 600))`.
6. If `flow == "login"`: `_create_or_get_user_from_oidc_claims()` → `_create_auth_response()`.
   If `flow == "link"`: load user by `state.user_id`, run existing logic from `account_linking._link_telegram_to_user()`.

### 6.3 EXISTING (modified): `POST /auth/telegram/oidc`

Popup flow. Add optional `nonce`:

```json
{
  "id_token": "string",
  "nonce": "string?",
  "campaign_slug": "string?",
  "referral_code": "string?"
}
```

If `nonce` is present, `validate_telegram_oidc_token(..., expected_nonce=nonce)` enforces equality with `claims["nonce"]`. If absent, the nonce check is skipped (back-compat for clients not yet upgraded).

### 6.4 DEPRECATED: `POST /auth/telegram/widget` and `POST /auth/telegram/link-widget`

Both return:

```
HTTP 410 Gone
{
  "detail": "Telegram Login Widget endpoint is deprecated. Use OIDC at /auth/telegram/oidc.",
  "migration_doc": "https://core.telegram.org/bots/telegram-login"
}
```

The handler still logs `warning` with `client_ip`, `user_agent` for observability. Functions and schemas are scheduled for removal in the second release after this one.

### 6.5 `account_linking.LinkTelegramRequest`

- Drop fields: `id`, `first_name`, `last_name`, `username`, `photo_url`, `auth_date`, `hash` (widget mode).
- Keep `init_data`, `id_token`.
- Add `nonce: str | None`.
- Add code-flow path: cabinet frontend calls `POST /auth/telegram/oidc/init` (JWT-authenticated, body `{"mode": "link"}`) → `POST /auth/telegram/oidc/callback`. The `link_telegram` endpoint remains usable for popup `id_token` mode.

## 7. Data flow

### 7.1 Popup login (existing + nonce)

```
Frontend                      Backend                         Telegram
   │ generate nonce, store local
   │ Telegram.Login.open({bot_id, nonce})
   │ ─────────────────────────────────────────────────►
   │                                                    auth UI
   │ ◄─────────────────────────────────────── id_token (JWT)
   │ POST /auth/telegram/oidc {id_token, nonce}
   │ ─────────────────────► validate JWKS + nonce
   │                        TokenReplayCache.add()
   │                        create/get user
   │ ◄───────────────────── AuthResponse
```

### 7.2 Authorization Code + PKCE (new)

```
Frontend                Backend                 Telegram
   │ POST /auth/telegram/oidc/init
   │ ────────────────►  generate state + verifier + nonce
   │                    store in OAuthStateCache (TTL 600 s)
   │ ◄──────────────── { authorize_url, state }
   │ window.location = authorize_url
   │ ────────────────────────────────────────────────► /auth?...&code_challenge=C&state=S
   │                                                user authorizes
   │ ◄──────────────────────────────────── 302 redirect_uri?code=X&state=S
   │ POST /auth/telegram/oidc/callback {code, state}
   │ ────────────────►  consume state (GETDEL)
   │                    POST oauth.telegram.org/token
   │                    (Basic auth client_id:client_secret)
   │                                              ► token endpoint
   │                    ◄──────────── { id_token, access_token }
   │                    validate JWKS + nonce
   │                    TokenReplayCache.add()
   │                    create/get user
   │ ◄──────────────── AuthResponse
```

### 7.3 Linking via code flow

Identical to 7.2, with two changes:
- `init` requires JWT and stores `flow="link"` and `user_id` in state data.
- `callback` loads user by `state.user_id` and runs the existing link path (with merge handling).

### 7.4 Deprecated widget

`POST /auth/telegram/widget` → log warning → return 410 Gone with migration link. No body validation, no DB hits.

## 8. Threat model

| Threat | Mitigation |
|--------|-----------|
| CSRF on callback | One-time `state` consumed via Redis `GETDEL`. |
| Authorization-code interception | PKCE S256 (`code_verifier` never in URL). |
| `id_token` replay | `TokenReplayCache` on `sha256(id_token)` + nonce binding to state. |
| `id_token` substitution | JWKS RS256 signature check + `iss == https://oauth.telegram.org` + `aud == client_id` + `exp` + nonce match. |
| Token endpoint MITM | HTTPS-only via httpx (system trust store). |
| Hijacked link state | `flow="link"` is required and `user_id` is bound to state at init time. |
| Brute force on init/callback | Rate limits (10/min/IP). |

Logging never persists raw `id_token` or `code_verifier`; only hashes and short prefixes appear in logs.

## 9. Files

### 9.1 Modified

| File | Change |
|------|--------|
| `app/cabinet/auth/telegram_auth.py` | Add `exchange_authorization_code()`. Extend `validate_telegram_oidc_token(id_token, client_id, expected_nonce=None)`. |
| `app/cabinet/auth/__init__.py` | Export `exchange_authorization_code`. |
| `app/cabinet/schemas/auth.py` | Add `TelegramOIDCInitRequest`, `TelegramOIDCInitResponse`, `TelegramOIDCCallbackRequest`. Extend `TelegramOIDCAuthRequest` with optional `nonce`. (Widget schema stays for one more release.) |
| `app/cabinet/routes/auth.py` | Add `oidc_init()` and `oidc_callback()`. Change `telegram_widget()` and `telegram_link_widget()` to return HTTP 410. Extend `telegram_oidc()` to accept `nonce`. |
| `app/cabinet/routes/account_linking.py` | Drop widget fields from `LinkTelegramRequest`, add `nonce`. Hook the code-flow path through `/oidc/init?mode=link` + `/oidc/callback`. |
| `app/cabinet/routes/branding.py` | Mark `TELEGRAM_WIDGET_*` as deprecated in the `auth_methods` payload. Expose new code-flow availability flag. |
| `app/config.py` | Add `TELEGRAM_OIDC_REDIRECT_URI: str = ''`. |
| `app/services/system_settings_service.py` | Add `TELEGRAM_OIDC_REDIRECT_URI` to the OIDC settings group with description, format, example. |
| `app/utils/cache.py` | Confirm `OAuthStateCache` already supports `code_verifier` + `nonce` payload keys. Extend if not. |

### 9.2 New

| File | Purpose |
|------|---------|
| `app/cabinet/static/telegram-login-test.html` | Manual test page with two buttons (popup, code) that exercise both flows and dump returned claims. |
| `tests/cabinet/auth/test_telegram_oidc_code_flow.py` | Unit + integration tests for the code-flow path, nonce validation, deprecated widget, and linking. |

### 9.3 Deletion (NOT this iteration — release N+2)

- `validate_telegram_login_widget()` function.
- `TelegramWidgetAuthRequest` schema.
- `/auth/telegram/widget` and `/auth/telegram/link-widget` endpoints.
- `TELEGRAM_WIDGET_*` settings group.

## 10. Error handling

### 10.1 Per endpoint

| Endpoint | Status | Trigger | Detail |
|----------|--------|---------|--------|
| `/oidc/init` | 400 | OIDC disabled in settings | `Telegram OIDC is not configured` |
| `/oidc/init` | 400 | `TELEGRAM_OIDC_REDIRECT_URI` empty | `Redirect URI not configured` |
| `/oidc/init` | 401 | `mode=link` without JWT | `Linking requires authentication` |
| `/oidc/init` | 429 | rate limited | `Too many requests`, `Retry-After: 60` |
| `/oidc/callback` | 400 | state missing or expired | `Invalid or expired OAuth state` |
| `/oidc/callback` | 400 | `state.provider != "telegram"` | `OAuth state was not initiated for Telegram` |
| `/oidc/callback` | 400 | flow mismatch | `OAuth state flow mismatch` |
| `/oidc/callback` | 502 | token endpoint timeout / 5xx | `Token exchange failed (upstream)` |
| `/oidc/callback` | 502 | token endpoint returned `error` | `Token exchange rejected: <error>` |
| `/oidc/callback` | 401 | id_token signature/aud/iss/exp invalid | `Invalid or expired Telegram OIDC token` |
| `/oidc/callback` | 401 | nonce mismatch | `Invalid OIDC nonce` |
| `/oidc/callback` | 401 | replay (TokenReplayCache hit) | `Invalid or expired Telegram OIDC token` |
| `/oidc/callback` | 429 | rate limited | as above |
| `/oidc` (popup) | 400 | OIDC disabled | as above |
| `/oidc` (popup) | 401 | nonce mismatch when nonce sent | `Invalid OIDC nonce` |
| `/widget`, `/link-widget` | 410 | always | `Telegram Login Widget endpoint is deprecated…` + `migration_doc` |

### 10.2 Edge cases

1. **Missing `client_secret`** → `oidc_init` returns 400 (`Server-side OIDC requires CLIENT_SECRET`). Popup flow continues to work without it.
2. **`oauth.telegram.org` blocked** for the user's network → `init` still succeeds, `window.location` fails client-side. The frontend is responsible for falling back to the existing deep-link bot auth.
3. **Token endpoint slow** → httpx timeout 10 s → 502 + structured log; retry requires a fresh `init`.
4. **State double-use race** → second callback with same state hits 400 (state already consumed). OK.
5. **Campaign / referral state but user already registered** → existing `_process_campaign_bonus` handles the duplicate registration check.
6. **Hijack link state with login flow** → `state.flow` check prevents this (init records flow at creation time).
7. **JWKS rotation during exchange** → existing `_force_refresh_jwks` with cooldown is sufficient.
8. **Clock skew on `auth_date`** → reuse `_MAX_CLOCK_SKEW_SECONDS = 300`.
9. **Popup without nonce** → `expected_nonce=None` → log info(`OIDC token validated without nonce — frontend should add nonce`); accept.

### 10.3 Logging

All 4xx and 5xx events use structured fields: `flow`, `state_prefix(8)`, `user_id?`, `client_ip`, `error_code`, `upstream_status?`. Raw `id_token` and `code_verifier` never appear in logs. Hashes only.

### 10.4 Rate limits

- `/oidc/init`: 10/min/IP (login) or 10/min/user (link).
- `/oidc/callback`: 10/min/IP.
- `/oidc` popup: 20/min/IP (matches current setting).

## 11. Settings

| Setting | Type | Default | Notes |
|---------|------|---------|-------|
| `TELEGRAM_OIDC_ENABLED` | bool | `false` | Existing. |
| `TELEGRAM_OIDC_CLIENT_ID` | str | `''` | Existing — bot numeric ID from BotFather → Web Login. |
| `TELEGRAM_OIDC_CLIENT_SECRET` | str | `''` | Existing — now actually used by code flow. |
| `TELEGRAM_OIDC_REDIRECT_URI` | str | `''` | **NEW** — fully-qualified `https://cabinet.example.com/auth/telegram/callback`. Must be registered in BotFather → Web Login → Allowed URLs. |
| `TELEGRAM_WIDGET_*` | various | — | Marked deprecated in admin UI. Removed in release N+2. |

## 12. Testing

### 12.1 Unit and route tests (`tests/cabinet/auth/test_telegram_oidc_code_flow.py`)

| Test | Coverage |
|------|----------|
| `test_pkce_challenge_generation` | verifier 43–128 chars url-safe, challenge = base64url(sha256(verifier)) without padding |
| `test_oidc_init_login_returns_authorize_url` | URL contains `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`, `nonce`, `scope=openid profile`, `response_type=code` |
| `test_oidc_init_link_requires_jwt` | without JWT → 401; with JWT → state contains `user_id` |
| `test_oidc_init_disabled` | `TELEGRAM_OIDC_ENABLED=false` → 400 |
| `test_oidc_init_no_redirect_uri` | empty `TELEGRAM_OIDC_REDIRECT_URI` → 400 |
| `test_callback_state_consumed_once` | second callback with same state → 400 |
| `test_callback_invalid_state` | random state → 400 |
| `test_callback_token_exchange_success` | httpx mock token endpoint → JWT → claims → user create |
| `test_callback_token_exchange_4xx` | mock 400 → 502 with upstream error in detail |
| `test_callback_token_exchange_timeout` | mock TimeoutException → 502 |
| `test_callback_id_token_invalid_sig` | bad JWT → 401 |
| `test_callback_nonce_mismatch` | claims.nonce ≠ state.nonce → 401 |
| `test_callback_replay` | second callback with same id_token → 401 |
| `test_callback_link_flow_uses_state_user_id` | flow=link callback loads user from state, does not create |
| `test_callback_link_flow_conflict` | telegram_id already linked elsewhere → merge_token |
| `test_validate_oidc_token_with_nonce` | popup endpoint with nonce → enforced |
| `test_validate_oidc_token_without_nonce` | popup without nonce → accepted (back-compat) |
| `test_widget_endpoint_returns_410` | `/auth/telegram/widget` → 410 + migration link |
| `test_link_widget_endpoint_returns_410` | `/auth/telegram/link-widget` → 410 |
| `test_link_telegram_widget_mode_removed` | LinkTelegramRequest with old widget fields → 422 |
| `test_oidc_init_rate_limit` | 11th request within 60 s → 429 |

### 12.2 Integration tests (mocked)

- `test_full_code_flow_e2e_mocked`: init → mocked redirect → callback → AuthResponse + tokens.
- `test_campaign_bonus_via_code_flow`: state carries campaign_slug → bonus applied.
- `test_referral_via_code_flow`: state carries referral_code → referral applied for new user.

### 12.3 Manual

- `app/cabinet/static/telegram-login-test.html` provides a clickable harness for popup and code flows. Static mount is verified at startup.

### 12.4 Existing tests

- Legacy widget tests are updated to expect HTTP 410.
- `test_link_telegram_widget` is deleted (widget mode removed from linking).

### 12.5 Coverage targets

- `app/cabinet/auth/telegram_auth.py`: ≥ 90 %.
- `oidc_init` / `oidc_callback` route handlers: ≥ 95 % (security-critical).

## 13. Migration / rollout

1. Land this change behind `TELEGRAM_OIDC_ENABLED=true` (already gated).
2. Set `TELEGRAM_OIDC_REDIRECT_URI` to the cabinet's HTTPS callback URL and register it in BotFather → Web Login → Allowed URLs.
3. Communicate the deprecation to the frontend team. Provide the static test page link.
4. Frontend switches widget script for `Telegram.Login.open` (popup) and the new code-flow route. Both can ship together.
5. Monitor 410 hits on `/widget` for one or two releases. Once near zero, remove deprecated handlers, schemas, and `TELEGRAM_WIDGET_*` settings.

## 14. Out of scope

- Mini-App `initData` flow.
- Deep-link bot auth fallback.
- Frontend implementation (separate repo).
- Removal of legacy widget code (deferred to release N+2).
- Phone-number capture (`phone` scope was rejected in brainstorming).
