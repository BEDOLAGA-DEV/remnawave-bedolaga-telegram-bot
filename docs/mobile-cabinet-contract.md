# Bedolaga Mobile Cabinet Contract

## Contract Version

Version: bedolaga-mobile-cabinet-v1
Owner: K2SO
Consumers: R2D2 iOS, C3PO Android
Stability rule: response fields used by mobile must not change without a new contract version and updated fixtures.
Legacy auth rule: contract endpoints must reject WebApiToken-only credentials and must not require `X-API-Key`.
Mobile auth predicate: valid cabinet JWT + role name in `Superadmin`, `Admin`, `Moderator` + endpoint permission when required.
Refresh validity rule: mobile receives deterministic refresh-token validity evidence through explicit `refresh_expires_in` on login and refresh responses.

## Endpoint Audit

| Capability | Current root endpoint | Cabinet endpoint/facade | Auth | Permission | Response contract | Decision | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Health check | `GET /health` | `GET /health` | none | none | Existing health reachability. | use-existing-cabinet-route | Root and cabinet share unauthenticated health behavior; no admin data. |
| Email login | none | `POST /cabinet/auth/email/login` | none | none | Existing auth response plus `expires_in` and `refresh_expires_in`. | use-existing-cabinet-route | `app/cabinet/routes/auth.py` issues cabinet JWTs and stores refresh token rows. |
| OAuth providers | none | `GET /cabinet/auth/oauth/providers` | none | none | Mobile must hide Telegram and only expose configured native-safe providers. | use-existing-cabinet-route | OAuth remains outside admin-token migration; Telegram is excluded by mobile contract. |
| OAuth authorize | none | `GET /cabinet/auth/oauth/{provider}/authorize` | none | none | Provider state/PKCE/deep-link rules are platform-gated before mobile enables OAuth. | use-existing-cabinet-route | Existing cabinet OAuth routes own state/callback validation. |
| OAuth callback | none | `POST /cabinet/auth/oauth/{provider}/callback` | none | none | Invalid state/verifier is rejected by cabinet OAuth flow. | use-existing-cabinet-route | Existing cabinet OAuth routes; no root Web API fallback. |
| Token refresh | `POST /cabinet/auth/refresh` | same | refresh JWT + stored token row | none | Returns `access_token`, original `refresh_token`, `expires_in`, `refresh_expires_in`; revoked/expired/inactive user returns 401. | use-existing-cabinet-route | `app/cabinet/routes/auth.py` verifies JWT type, token row, expiry/revocation, and active user. |
| Permission check | none | `GET /cabinet/auth/me/permissions` | cabinet JWT | authenticated user | Current roles/permissions from backend so mobile can allow only exact role names. | use-existing-cabinet-route | `PermissionService.get_user_permissions` reloads DB RBAC. |
| Ticket list/detail/reply/status | `/tickets*` | `/cabinet/admin/mobile/tickets*` | cabinet JWT + mobile allowed role | `tickets:read`, `tickets:reply`, `tickets:close` | Existing cabinet ticket shapes behind mobile role-gated wrappers. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`; wrappers compose mobile role allowlist with existing permission service behavior. |
| User search/detail/by Telegram ID | `/users*` | `/cabinet/admin/mobile/users*` | cabinet JWT + mobile allowed role | `users:read` | Existing cabinet user shapes behind mobile role-gated wrappers; Telegram ID path is `/users/by-telegram/{telegram_id}`. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`; wrappers compose mobile role allowlist with existing permission service behavior. |
| Subscription lookup by URL | `GET /subscriptions` paging/search | `GET /cabinet/admin/mobile/subscriptions/lookup?subscription_url=...` | cabinet JWT + mobile allowed role | `users:read` | `MobileSubscriptionResponse`. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`. |
| Subscription detail | `GET /subscriptions/{id}` | `GET /cabinet/admin/mobile/subscriptions/{subscription_id}` | cabinet JWT + mobile allowed role | `users:read` | `MobileSubscriptionResponse`. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`. |
| Monthly income/global transactions | `GET /transactions` | `GET /cabinet/admin/mobile/transactions/monthly-income` | cabinet JWT + mobile allowed role | `stats:read` | Completed deposit transactions filtered to `REAL_PAYMENT_METHODS`, calendar-month bounds. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`. |
| User transactions/spending | `GET /transactions?user_id=...` | `GET /cabinet/admin/mobile/users/{user_id}/transactions` | cabinet JWT + mobile allowed role | `users:read` | `MobileTransactionListResponse`, completed real-payment deposits only. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`. |
| Balance update | `POST /users/{id}/balance` | `POST /cabinet/admin/mobile/users/{user_id}/balance` | cabinet JWT + mobile allowed role | `users:balance` | Existing response returns balance delta; mobile should refresh user detail after success if it needs a full user. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`; wrapper composes mobile role allowlist with existing permission service behavior. |
| Delete/reset subscription | `DELETE /users/{id}/subscription` | `POST /cabinet/admin/mobile/users/{user_id}/reset-subscription` | cabinet JWT + mobile allowed role | `users:subscription` | Existing reset-subscription semantics wrapped with `contract_version`. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`; deletes all user subscriptions and optionally disables panel user. |
| Media upload/download | `POST /upload`, `GET /media/{file_id}` | `POST /cabinet/admin/mobile/media/upload`, `GET /cabinet/media/{file_id}?token=...` | upload: cabinet JWT + mobile allowed role; download: signed media token | upload: `tickets:reply` | Downloads use expiring HMAC token bound to file ID; no admin-token header. Scriptable uploads are rejected. | add-mobile-facade | Upload wrapper is implemented in `app/cabinet/routes/admin_mobile.py`; signed download remains token-only in `app/cabinet/routes/media.py`. |
| Dashboard/widget stats | `GET /stats/full` | `GET /cabinet/admin/mobile/stats/full` | cabinet JWT + mobile allowed role | `stats:read` | `MobileDashboardStatsResponse`, cabinet-JWT replacement of root stats shape. | add-mobile-facade | Implemented in `app/cabinet/routes/admin_mobile.py`. |
| Realtime tickets | `GET /ws?api_key=...` | `GET /cabinet/admin/mobile/realtime` | cabinet JWT + mobile allowed role | `tickets:read` | Explicit disabled feature response; mobile must not connect to root or cabinet query-token WebSockets. | disable-mobile-feature | Mobile v1 disables realtime until a non-query-token WS contract is reviewed. |
| CORS settings | `PUT /settings/{key}` | `GET /cabinet/admin/mobile/settings/cors`, `PUT /cabinet/admin/mobile/settings/cors/{key}` | cabinet JWT + mobile allowed role | read: `settings:read`, edit: `settings:edit` | Pre-auth local/operator guidance only; server edit limited to `WEB_API_ALLOWED_ORIGINS` and `CABINET_ALLOWED_ORIGINS` after env-lock checks. | operator-guidance-only | Implemented CORS contract wrapper delegates env-lock and secret masking to cabinet settings service. |

## Mobile Auth Predicate

Every authenticated endpoint under `/cabinet/admin/mobile` requires:

1. a valid cabinet JWT access token,
2. current database role name exactly `Superadmin`, `Admin`, or `Moderator`,
3. matching current database RBAC permission when the endpoint requires one.

The predicate reloads roles and permissions from the database on each request. JWT-embedded roles or permissions are not accepted as authority for mobile admin access, so downgraded users are rejected on the next request.

## Refresh-Token Validity

Method: `explicit-fields`.

Login and refresh responses include:

- `expires_in`: access-token lifetime in seconds.
- `refresh_expires_in`: refresh-token remaining lifetime in seconds.

Refresh still returns 401 for invalid JWT type/signature, expired refresh JWT, missing or revoked refresh-token row, expired token row, or inactive/deleted user. Mobile must schedule foreground/background refresh from `expires_in`, then stop on refresh 401 or failed role revalidation.

## WebSocket Decision

Decision: `disable-realtime-for-mobile-v1`.

R2D2/C3PO must not use `GET /ws?api_key=...` and must not use existing `/cabinet/ws?token=...` query-token WebSocket for the mobile contract. A future realtime contract must use a non-query bearer mechanism and the same current-state role predicate.

## Media Auth Model

Uploads use `POST /cabinet/admin/mobile/media/upload` with cabinet JWT, mobile role allowlist, and `tickets:reply`. Downloads use `GET /cabinet/media/{file_id}?token=<signed_media_token>`. The token is HMAC-signed, file-bound, expiring, and does not require `X-API-Key` or an Authorization header. Scriptable upload extensions and content types are rejected.

## Fixtures

Fixture directory: `tests/fixtures/bedolaga-mobile-cabinet-v1`.

Each fixture includes `contract_version`, method/path/status, auth mode, required role names, required permissions, response schema, and fixture kind.
