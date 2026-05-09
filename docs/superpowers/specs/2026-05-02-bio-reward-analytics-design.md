# Bio-Reward Analytics

## Context

The bio-reward feature is shipping. Operators need to measure (a) whether bio
participation actually moves the needle on revenue (conversion to paid subs)
and (b) how much organic growth the program produces (viral K-factor from
referrals attributed to bio-active users). Without these numbers, the program
is uncalibrated — discount levels and grace windows are guesswork.

This adds two metrics, displayed inside the existing in-bot admin panel under
Bio-Reward → Аналитика. Computed periodically and cached so the admin UI
renders instantly.

## User-confirmed decisions

| Topic | Decision |
|---|---|
| Display location | Bot admin panel only (extends `app/handlers/admin/bio_reward.py`). Cabinet REST API not in scope for this spec. |
| Conversion cohorts | Monthly **and** weekly. Admin toggles between them. |
| Viral coefficient | Three rolling windows: 7d, 30d, 90d. All shown together. |
| Compute mode | Precomputed via daily scheduler tick; results cached in a snapshot table. |
| Backfill | On first run, compute all historical cohorts since the earliest opt-in. |

## Metrics

### d1 — Conversion cohort

Group `BioRewardParticipant` rows by `opted_in_at` truncated to month or week.
For each bucket compute:
- `total_opted_in`           — count of participants in the bucket.
- `ever_active`              — count whose status was ever ACTIVE (proxy: `last_bio_seen_at IS NOT NULL`).
- `converted_paid`           — count of users in the bucket whose ANY non-trial subscription has `created_at >= opted_in_at`.
- `conversion_pct`           — `converted_paid / total_opted_in * 100`, integer rounded.
- `total_paid_revenue_kopeks` — sum of `Transaction.amount_kopeks` where `type = SUBSCRIPTION_PAYMENT` and `created_at >= opted_in_at` for users in the bucket.
- `avg_days_to_convert`       — mean of (first paid `created_at` − `opted_in_at`).days for converters; null if zero converters.

### d2 — Viral coefficient (K-factor)

For window W ∈ {7, 30, 90} days ending at the snapshot computed_at:
- `bio_active_users` — distinct user_ids in `BioRewardParticipant` with `status = ACTIVE` at any point during the window (proxy: `last_bio_seen_at` falls within window).
- `attributed_referrals` — count of `User` rows where `referred_by_id` belongs to a bio-active user **and** `User.created_at` falls within the window.
- `k_factor` — `attributed_referrals / bio_active_users` as float (0 if denominator is 0).
- `paid_attributed_referrals` — subset of `attributed_referrals` where the referred user has any non-trial subscription.

## Data model

New table `bio_reward_analytics_snapshot`:

| Column | Type | Notes |
|---|---|---|
| id | int PK | autoincrement |
| snapshot_type | varchar(40) | `conversion_monthly`, `conversion_weekly`, `viral` |
| bucket_key | varchar(40) | `2026-05`, `2026-W18`, `7d`/`30d`/`90d` |
| payload | JSON | the computed metric dict |
| computed_at | timestamptz | when it was last refreshed |

Composite UNIQUE on (`snapshot_type`, `bucket_key`); upsert on recompute.
Index on `(snapshot_type, bucket_key DESC)` for the cohort grid query.

## Service layer

`app/services/bio_reward_analytics.py` (new):

- `compute_conversion_cohorts(db, granularity)` — returns list of dicts keyed by bucket; one call per granularity.
- `compute_viral_coefficient(db, window_days)` — returns dict for one window.
- `recompute_all(db)` — orchestrator: monthly cohorts + weekly cohorts + 3 viral windows. Upserts into snapshot table.
- Each metric is a single SQL query with GROUP BY / aggregates so cost stays bounded even at scale.

Pure-function helpers (testable without DB):
- `bucket_for_month(dt)` → `"YYYY-MM"`.
- `bucket_for_week(dt)` → `"YYYY-Www"` ISO week.

## Scheduler

Hook into the existing `BioRewardService.start_monitoring` loop. Track
`_last_analytics_run_at`; if the timestamp is missing or older than 24h, call
`recompute_all` after the regular bio-check pass. This avoids adding a new
asyncio task and keeps the analytics tick cheap.

## Admin UI

Extend `app/handlers/admin/bio_reward.py`:

- New row on the config keyboard: `📊 Аналитика` → callback `br_admin_analytics`.
- Analytics screen has 3 sub-buttons:
  - `📅 Cohorts (monthly)` → callback `br_admin_analytics_cohorts:monthly`
  - `📅 Cohorts (weekly)`  → callback `br_admin_analytics_cohorts:weekly`
  - `🚀 Viral coefficient` → callback `br_admin_analytics_viral`
- Cohort screens render a Markdown table (last 12 buckets, newest first) with
  columns: bucket, opted_in, converted, conv %, revenue ₽, avg days.
- Viral screen renders one card per window (7d/30d/90d) with K-factor +
  attributed referrals + paid-attributed referrals.
- All screens have `🔄 Пересчитать сейчас` to force `recompute_all` and a back
  button to the bio-reward config panel.

## Files to create / modify

Create:
- `migrations/alembic/versions/0079_add_bio_reward_analytics.py`
- `app/services/bio_reward_analytics.py`
- `tests/test_bio_reward_analytics.py`

Modify:
- `app/database/models.py` — add `BioRewardAnalyticsSnapshot` model.
- `app/services/bio_reward_service.py` — call `recompute_all` from scheduler tick when due.
- `app/handlers/admin/bio_reward.py` — analytics submenu + handlers.

## Verification

1. Unit tests for `bucket_for_month` / `bucket_for_week` + a tiny synthetic
   conversion-rate calc.
2. Integration smoke: seed 5 participants opted_in across 2 months, 3 of them
   later get a non-trial subscription → expect monthly bucket conversion_pct
   matches the manually computed value.
3. Manual check: open admin panel → Аналитика → confirm screens render and
   `🔄 Пересчитать` updates `computed_at`.
4. Performance: `recompute_all` finishes in < 2 s on a DB with 10k participants
   (one EXPLAIN check during dev).
