# Phase 2 Deep Dive — 2026-04-27

Three risk classes:
- (a) Money-path race conditions
- (b) Auth bypass / IDOR
- (c) Webhook signature verification

## (a) Money-path race conditions

### Mutations enumerated

Helper-layer locking: `add_user_balance` (`app/database/crud/user.py:469`) and `subtract_user_balance` (line 597) BOTH internally re-fetch the user with `with_for_update()` and `populate_existing=True` before mutating `balance_kopeks`. Every caller of these wrappers is therefore lock-safe regardless of whether the outer code locked the user. `lock_user_for_pricing` / `lock_user_for_update` (lines 576/447) provide the same row lock for code paths that mutate `balance_kopeks` directly.

Inventory by category (raw `balance_kopeks` += / -= sites only — wrapper-mediated mutations omitted because the wrapper re-locks):

- **Webhook-fed credits** (15 providers): `cloudpayments.py:292`, `aurapay.py:419`, `freekassa.py:319`, `heleket.py:364`, `kassa_ai.py:315`, `paypear.py:408`, `cryptobot.py:312`, `platega.py:425`, `rollypay.py:414`, `mulenpay.py:296`, `severpay.py:410`, `pal24.py:423`, `stars.py:424`, `wata.py:517`, `yookassa.py:769`, `tribute_service.py:154,273,426`. **VERIFIED SAFE**: every webhook calls `lock_user_for_update(db, user)` immediately before the mutation, and every webhook checks idempotency via the payment row (`is_paid` flag under `SELECT FOR UPDATE`) and/or `get_transaction_by_external_id` before crediting. The unique constraint `uq_transaction_external_id_method` (`models.py:1665`) provides DB-level idempotency belt.
- **Bot-side debits** (subscription, traffic, tariff): all routed through `subtract_user_balance` (which locks). Direct mutations only appear in refund branches: `subscription/devices.py:624,640,1352`, `cabinet/.../devices.py:215,469`, `webapi/.../miniapp.py:6183`, `subscription_auto_purchase_service.py:1673`. **VERIFIED SAFE**: every refund branch acquires `with_for_update()` on the user row first.
- **Internal transfers**: `services/account_merge_service.py:641` (locks both rows via `lock_user_for_update` line 637-638), `cabinet/contests.py:142` (locked at line 141), `services/contests/attempt_service.py:325` (locked at 324), `services/wheel_service.py:321` (locked at 315), `services/referral_withdrawal_service.py:506` (locked at 496), `handlers/admin/referrals.py:636` (locked at 635). **VERIFIED SAFE**.
- **Counter mutations**: `services/promocode_service.py:193` (atomic SQL UPDATE — safe), `services/promocode_service.py:521` (read-then-write deactivation, **NOT** locked).
- **Unlocked direct mutations** (real findings): `database/crud/achievement.py:492` (achievement reward), `handlers/stars_payments.py:660,680` (Stars refund handler).

Verified safe: 35+ direct mutations + ~70 wrapper calls (all `add_user_balance`/`subtract_user_balance` callers). Phase 1 over-counted: of the 13 webhook P7 hits, all 13 are locked (Phase 1 sweep regex did not detect the `lock_user_for_update(db, user)` lines that immediately precede the `+=` line); of the 5 non-webhook P7 hits, only 2 were genuinely unlocked.

### Findings

| # | File | Line | Description | Severity | Reproducer | Patch / Defer |
|---|------|------|-------------|----------|-----------|---------------|
| 1 | app/handlers/stars_payments.py | 660,680 | Stars refund handler reads `user.balance_kopeks` and writes `-=`/`+=` without `lock_user_for_update`; no idempotency check on `refund.telegram_payment_charge_id` (existing-Transaction lookup is for resolving user_id only, not for skipping duplicate processing). Concurrent or replayed Telegram refund event could double-debit balance. | high | Telegram retries refund webhook on transient failure; both invocations read same `balance_kopeks`, both compute `unused_kopeks` from same subscription state, both write — second invocation deducts twice. | Patch applied: lock user row, add idempotency check via `get_transaction_by_external_id(db, f'refund_{charge_id}', PaymentMethod.TELEGRAM_STARS)`. |
|   |   |   | **Follow-up patch (Phase 2a-fix)**: original patch had two residual blockers — (a) TOCTOU between idempotency lookup and `lock_user_for_update` (both webhooks could pass the check before either acquired the lock), and (b) `create_transaction(...)` for the refund-marker ran AFTER `db.commit()` on the debit, so a crash in between would leave the user debited with no idempotency marker (next redelivery double-debits). Re-ordered: lock → idempotency check (returns with `db.rollback()` to release lock cleanly when already-processed) → debit/subscription → `create_transaction(..., commit=False)` (uses `db.flush()` so any unique-constraint violation surfaces synchronously) → single `db.commit()` → `emit_transaction_side_effects(...)` (replaces the side effects that are skipped under `commit=False`). Debit and refund-marker now land in the same transaction; no TOCTOU window remains. | | | |
| 2 | app/database/crud/achievement.py | 488-499 | `check_and_unlock_all` loads user without `lock_user_for_update`, then increments `user.balance_kopeks` for each `balance_kopeks` reward. Two concurrent triggers (e.g. user opens cabinet while a webhook also recomputes achievements) both observe the same `unlocked_ids` and both attempt the credit. | medium | Unique constraint `uq_user_achievement` causes the loser's `db.commit()` to IntegrityError → entire transaction rolls back, so balance increment is undone. Bounded blast radius — but a partial commit between unlock and `db.commit()` could still leak. | Patch applied: lock user with `lock_user_for_update` at start of unlock loop. |
| 3 | app/services/promocode_service.py | 521 | `deactivate_discount_promocode` reads `promocode.current_uses` then writes `-= 1` without `with_for_update()` on the promocode row or user row. Two concurrent deactivations race on counter. | low | Two admin-initiated deactivations of the same code in same window: both read same `current_uses`, both write `current_uses - 1`, counter drifts. No financial impact (counter only). | Patch applied: lock promocode + user via `with_for_update()` before reading. |
|   |   |   | **Follow-up patch (Phase 2a-fix)**: original patch acquired `lock_user_for_update` early but the `no_active_discount_promocode` early-return path at ~line 500 left the FOR UPDATE lock open until session close (lock leak). Added `await db.rollback()` immediately before the early `return` to release the lock cleanly. The `discount_already_expired` path at ~line 511 already calls `await db.commit()` so it was unaffected. | | | |

## (b) Auth bypass / IDOR

### Cabinet admin routes audited

Re-confirmation pass over Phase 1 P6 result: 41 admin route files under `app/cabinet/routes/admin_*.py`, 367 cumulative `require_permission`/`require_admin` calls, every file has at least one. Newly-added file `admin_help.py` (post-Phase-1) verified — 7 routes, all gated with `Depends(require_permission('help:read'|'help:create'|'help:edit'|'help:delete'))`. **Status: 0 unprotected admin routes.**

### Bot admin handlers audited

Inventory: 1040 `def`/`async def` total across 45 files in `app/handlers/admin/`, 717 `@admin_required` decorations across 44 files. The 323-function delta breakdown:

- **`app/handlers/admin/tickets.py`** (16 handlers, 0 `@admin_required`): uses inline gate `if not (settings.is_admin(...) or SupportSettingsService.is_moderator(...)): return ACCESS_DENIED` because the moderator role coexists with admin. All 13 user-callable handlers verified to have the inline gate (lines 90, 158, 190, 363, 381, 545, 610, 680, 704, 734, 922, 976, 1157). Helper `notify_user_about_ticket_reply` (line 1028) is called only from authenticated `handle_admin_ticket_reply`.
- **FSM `process_*` handlers** (e.g. `promo_offers.py:2321-2341`, `monitoring.py:1644,1961`, `pricing.py:1068`, `promo_groups.py:625-686`, `promocodes.py:557,642,864`): registered via `@router.message(AdminStates.editing_*)` — state can only be entered from `@admin_required` parent handlers, so functionally protected via FSM state isolation.
- **Private helpers** (`refresh_server_selection_screen` at `users.py:3683`, `safe_edit_or_send_text` at `messages.py:54`, `show_media_preview` at `messages.py:879`): not registered as router handlers; called from authenticated parents.

One minor inconsistency noted: `admin_delete_message` inline at `tickets.py:1216` has no permission gate but uses `chat_id=callback.from_user.id` so users can only delete messages in their own chat — self-only operation, not admin-privileged. Not a finding.

**Status: 0 genuine gaps.**

### IDOR endpoints

193 routes with `{path}` parameters across all cabinet route files. Excluded the 155 admin-prefixed routes (already gated by `require_permission`). Audited the remaining 38 non-admin routes:

- **Ownership-checked correctly** (33 routes): `polls.py` 3, `tickets.py` 2, `user_notifications.py` 2, `ticket_notifications.py` 2, `withdrawal.py` 1 (also acquires `with_for_update()`), `gift.py` 1, `balance.py` 4, `partner_application.py` 1, `account_linking.py` 3, `auth.py` 3, `subscription_modules/multi_tariff.py` 2, `subscription_modules/devices.py` 1 (via `resolve_subscription` → `get_subscription_by_id_for_user(db, sid, user.id)`).
- **By-design unauthenticated public endpoints** (4 routes, with rate-limiting): `landing.py` `/purchase/{token}`, `/activate/{token}`, `/{slug}`, `/{slug}/purchase`. Token-secrecy model.
- **Unauthenticated by-design auth flows** (2 routes): `oauth.py` `/{provider}/authorize`, `/{provider}/callback` — public OAuth bootstrap with state-token CSRF protection.
- **Public read-only content** (3 routes): `help.py` `/{slug}`, `/{slug}/feedback`, `news.py` `/{slug}`, `info.py` `/faq/{page_id}` — `is_published` filter, no user-owned data.
- **Contest game data** (2 routes): `contests.py` `/{round_id}`, `/{round_id}/answer` — round is global; per-user `attempt` row enforces "one play per user" via `get_attempt(db, round_id, user.id)`. Not a true IDOR.

**1 IDOR finding — see row 4 below.**

### JWT verification

`app/cabinet/auth/jwt_handler.py:97` decoder:
- **Algorithm pinned**: `algorithms=[JWT_ALGORITHM]` where `JWT_ALGORITHM = 'HS256'` — algorithm-confusion safe.
- **`audience` / `issuer`**: not set on encode, not validated on decode — symmetric, no validation gap (would only matter if the same secret were shared with another service).
- **`exp` enforced**: PyJWT default `verify_exp=True`, no `options={'verify_exp': False}` override anywhere in the codebase.
- **Signing secret source**: `settings.get_cabinet_jwt_secret()` returns `CABINET_JWT_SECRET` if set, otherwise falls back to `BOT_TOKEN` with `warnings.warn()` AND `logger.warning('CABINET_JWT_SECRET not set, falling back to BOT_TOKEN')`. The fallback warns on every JWT operation (every authenticated request) — operationally noisy but not silent. Not a startup failure. See finding row 6.
- **Refresh token rotation**: original code returned the SAME refresh token on every `/auth/refresh` call (`refresh_token=request.refresh_token` at the prior `auth.py:1521`). Refresh tokens never rotate — long-lived token reuse. See finding row 5 (patched: revoke + reissue).

Other JWT sites verified:
- `app/cabinet/auth/telegram_link.py:48` (one-time linking token decode) — pinned `algorithms=[JWT_ALGORITHM]`, type-checked.
- `app/cabinet/auth/telegram_auth.py:256` (Telegram OIDC RS256 id_token) — pinned `algorithms=['RS256']`, validates `audience=client_id`, `issuer=_OIDC_ISSUER`, requires `['exp', 'iat', 'iss', 'aud', 'sub']`. Best-practice.

### Findings

| # | File | Line | Description | Severity | Reproducer | Patch / Defer |
|---|------|------|-------------|----------|-----------|---------------|
| 4 | app/cabinet/routes/media.py | 148 | `download_media` had NO `Depends(get_current_cabinet_user)` and NO ownership check on the Telegram `file_id` path parameter. Any unauthenticated request could fetch any file the bot has uploaded if the `file_id` were known/guessed/leaked, including: ticket-message attachments belonging to OTHER users (PII), pinned-message media (admin-uploaded), and broadcast-history media. | high | `curl -s 'https://cabinet.example/api/cabinet/media/AgACAgIAAxkBAA...'` returns the file with no auth header and no membership check. Telegram file_ids leak via logs, screenshots, and shared URLs. | Patch applied: added `user: User = Depends(get_current_cabinet_user)` + `db: AsyncSession = Depends(get_cabinet_db)` deps; before passing the `file_id` to `bot.get_file`, query `TicketMessage` joined to `Ticket` for any row with `media_file_id == file_id` AND (`ticket.user_id == user.id` OR `ticket_message.user_id == user.id`); if none and the user is not admin/moderator, raise 404 (uniform — prevents enumeration oracle). Staff bypass uses `settings.is_admin(...) or SupportSettingsService.is_moderator(...)` matching the pattern in `app/handlers/admin/tickets.py`. |
| 5 | app/cabinet/routes/auth.py | 1457 | `/auth/refresh` endpoint did NOT rotate the refresh token. It validated the presented refresh token, then returned a new access token alongside `refresh_token=request.refresh_token` — same hash, same DB row, never revoked. Effective token lifetime equals the DB `expires_at` (default 30 days) regardless of how many times it is used. If the refresh token leaks (XSS, browser cache, shared device), the attacker has 30-day persistent access with no way to invalidate via natural use. OWASP recommends rotation on every refresh. | high | Capture a refresh token from a victim's browser; call `/auth/refresh` with it repeatedly for 30 days — each call returns a fresh access token; the original refresh row in `cabinet_refresh_tokens` keeps `revoked_at IS NULL` until manual logout. | Patch applied (in-place rotation, no schema work needed — `cabinet_refresh_tokens.revoked_at` already exists): on a successful refresh, mark the presented `token_record.revoked_at = datetime.now(UTC)`, mint a new refresh token via `create_refresh_token(user.id)`, insert a new `CabinetRefreshToken` row with the new hash and same `device_info`, and commit both updates in the same transaction. Return the new refresh token to the caller. |
| 6 | app/config.py | 2943 | `get_cabinet_jwt_secret()` falls back to `BOT_TOKEN` when `CABINET_JWT_SECRET` is unset, emitting a `warnings.warn(UserWarning)` and `logger.warning(...)` on every call (every authenticated request). Two consequences: (a) operational noise (warning floods logs in non-production environments); (b) using the bot token as a JWT signing key means anyone who can read the bot token (e.g. another service in the deployment) can forge cabinet JWTs. Not silent (warns) but operationally weak. | medium | Deploy without `CABINET_JWT_SECRET`; observe `logger.warning` in every request log; confirm bot-token-to-JWT confusion vector. | Defer to documentation/deployment hardening: add a startup-time check that hard-fails when `CABINET_JWT_SECRET` is unset AND `ENVIRONMENT == production` (rather than per-request warning). Queued — touches startup wiring and config validation, scope beyond batch-7. |

## (c) Webhook signature verification

### Webhooks audited
(filled in Task 2.9)

### Findings

| # | Provider | File | Verifies signature? | Replay protection? | IP allow-list? | Severity | Patch / Defer |
|---|----------|------|---------------------|--------------------|-----------------|----------|---------------|

## Summary

(filled at end of Phase 2)
