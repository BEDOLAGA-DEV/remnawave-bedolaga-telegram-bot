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
(filled in Task 2.4)

### Bot admin handlers audited
(filled in Task 2.5)

### IDOR endpoints
(filled in Task 2.6)

### JWT verification
(filled in Task 2.7)

### Findings

| # | File | Line | Description | Severity | Reproducer | Patch / Defer |
|---|------|------|-------------|----------|-----------|---------------|

## (c) Webhook signature verification

### Webhooks audited
(filled in Task 2.9)

### Findings

| # | Provider | File | Verifies signature? | Replay protection? | IP allow-list? | Severity | Patch / Defer |
|---|----------|------|---------------------|--------------------|-----------------|----------|---------------|

## Summary

(filled at end of Phase 2)
