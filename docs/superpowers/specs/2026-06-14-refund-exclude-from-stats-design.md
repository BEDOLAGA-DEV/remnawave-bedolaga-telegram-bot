# Refund marking — exclude refunded payments from statistics

**Date:** 2026-06-14
**Status:** Approved (design)

## Problem

Money statistics (revenue, sales, per-user spend, achievement conditions, referral
revenue) sum `Transaction` rows by type/payment-method and **ignore refunds**: a
payment that was refunded still counts as revenue/spend. There is no admin way to
mark a payment as refunded, and no link from a refund to its original transaction.

## Goal

Let an admin mark a payment transaction as **refunded** in the bot, and exclude
every refunded transaction from **all** money statistics. The money that was
returned stops inflating the numbers.

## Decisions (from brainstorming)

- **Trigger:** admin marks a transaction refunded (internal accounting). No payment
  provider refund API; the admin returns the real money out of band.
- **Effect:** mark + exclude from statistics only. Do **not** touch the user's
  balance, subscription, or referral earnings.
- **Surface:** bot admin panel only.

## Non-goals

- Issuing real refunds via provider APIs (YooKassa/CryptoBot/Stars/etc.).
- Deducting balance, revoking subscriptions, or clawing back referral bonuses.
- Auto-marking the existing event-driven refunds (Apple IAP / Tribute / renewal
  rollback). The admin can mark those manually; auto-wiring is a later task.
- Cabinet (web) UI.

## Components

### 1. Data model
Add to `Transaction` (`app/database/models.py`):
- `is_refunded: bool` — default `False`, `server_default='false'`, not null.
- `refunded_at: datetime | None` — nullable (AwareDateTime).
- `refunded_by: int | None` — admin `User.id` who marked it, nullable.

Alembic migration adds the three columns (next revision after the current head).

### 2. CRUD (`app/database/crud/transaction.py`)
- `mark_transaction_refunded(db, transaction_id, admin_id) -> Transaction | None`
  — sets `is_refunded=True`, `refunded_at=now`, `refunded_by=admin_id`; idempotent.
- `unmark_transaction_refunded(db, transaction_id) -> Transaction | None`
  — clears the three fields (admin mistake recovery).

### 3. Statistics exclusion
Add `Transaction.is_refunded.is_(False)` to **every** money aggregate that sums
real payments:
- `app/database/crud/transaction.py`: `get_transactions_statistics` (income),
  `get_user_total_spent_kopeks`.
- `app/cabinet/routes/admin_stats.py`: `get_transactions_statistics` (revenue block).
- `app/cabinet/routes/admin_sales_stats.py`: total revenue, addon revenue, renewals
  count, and any other money sum keyed on `Transaction`.
- `app/database/crud/achievement.py` `_get_user_stat`: `total_spent_kopeks`,
  `single_topup_max_kopeks`, `topup_count`, `referral_revenue_kopeks`.

The filter is additive (extra AND clause); it never changes results when nothing is
refunded, so existing behaviour is preserved until an admin marks something.

### 4. Bot admin UI (`app/handlers/admin/...`)
From the admin user card, show the user's real payments (DEPOSIT /
SUBSCRIPTION_PAYMENT). Tapping a payment offers **↩️ Пометить возвратом** (or
**↩️ Отменить возврат** if already refunded) with a confirm step. The handler calls
the CRUD and re-renders the list with a ✅/↩️ marker on refunded rows.

## Error handling

- Mark/unmark on a missing transaction id → answer "транзакция не найдена".
- Marking an already-refunded transaction is a no-op (idempotent) — UI just reflects
  state.
- Stat filter is a pure AND clause; cannot break existing queries.

## Testing

- CRUD: `mark_transaction_refunded` sets the three fields; `unmark_` clears them
  (fake/mock session or a constructed `Transaction`).
- Statistics: source-inspection tests (matching the achievement-regression style)
  asserting each listed money-aggregate function contains an `is_refunded` filter,
  so a future edit can't silently drop the exclusion.

## Files touched (anticipated)

- `app/database/models.py` — 3 columns on `Transaction`.
- `migrations/alembic/versions/<rev>_add_transaction_refund_flag.py` — migration.
- `app/database/crud/transaction.py` — mark/unmark + stat filters.
- `app/cabinet/routes/admin_stats.py`, `app/cabinet/routes/admin_sales_stats.py` —
  stat filters.
- `app/database/crud/achievement.py` — `_get_user_stat` filters.
- `app/handlers/admin/*` — refund mark/unmark UI in the user card; registration.
- `tests/` — CRUD tests + source-inspection stat-filter tests.
