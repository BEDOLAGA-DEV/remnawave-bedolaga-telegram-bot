# Auto-activate achievements (background sweep)

**Date:** 2026-06-14
**Status:** Approved (design)

## Problem

Achievements are evaluated and unlocked only by `check_and_unlock_all(db, user_id, bot=)`
(`app/database/crud/achievement.py`), which is called from exactly one place: the
achievements menu handler (`app/handlers/achievements.py:89`). So an achievement the user
already earned stays locked until they open the menu. Time-based conditions
(`days_active`, `registered`) never unlock on their own.

## Goal

Unlock achievements (and credit their rewards + notify the user) automatically in the
background, without the user opening the menu. Cover all condition types, including the
purely time-based ones.

## Non-goals

- Per-event hooks (unlock instantly after a purchase/top-up/referral). The background
  sweep already covers every condition type, including time-based ones that no event
  could trigger. Event hooks are extra surface for marginal immediacy — out of scope.
- Changing the achievement conditions, rewards, or `check_and_unlock_all` logic itself.
- Sweeping fully-inactive users (kept out of scope by the "active/recent" candidate set).

## Approach

Add a periodic sweep to the existing monitoring cycle that runs `check_and_unlock_all`
for an "active/recent" candidate set each cycle. The menu-open check stays as the instant
path. Cost is naturally bounded: `check_and_unlock_all` skips already-unlocked templates
*before* running any per-condition stat query, so users who completed everything are cheap;
only users with pending achievements incur stat queries.

## Components

1. **Candidate selection** — new crud helper in `app/database/crud/achievement.py`:

   ```
   async def get_achievement_sweep_user_ids(db, active_days: int) -> list[int]:
       # Union of:
       #   - user_ids with an active/trial subscription
       #   - User.status == 'active' AND User.updated_at >= now - active_days days
       # Both filtered to telegram_id IS NOT NULL.
       # id-then-fetch style (no SELECT DISTINCT on User rows — the users table has a
       # json column with no eq operator; see _check_low_balance_alerts note).
   ```

2. **Background sweep** — new `_check_achievements(self, db)` in
   `app/services/monitoring_service.py`, appended to `_monitoring_cycle` after the other
   `_check_*` calls:
   - Gate: return early unless `settings.ACHIEVEMENTS_ENABLED` **and**
     `settings.ACHIEVEMENTS_AUTO_CHECK_ENABLED`, and `self.bot` is set.
   - Fetch candidate ids via the helper (active_days = `ACHIEVEMENTS_SWEEP_ACTIVE_DAYS`).
   - For each id, in batches: open a fresh `AsyncSessionLocal`, call
     `check_and_unlock_all(session, uid, bot=self.bot)`, `commit()`. Per-user `try/except`
     (a single user's error logs a warning and does not abort the sweep). The fresh
     per-user session keeps the user-row `FOR UPDATE` lock short and isolates failures from
     the monitoring cycle's own transaction. A small `asyncio.sleep` between batches.
   - Log a summary (candidates scanned, users with new unlocks).

3. **Settings** — add to `app/config.py`:
   - `ACHIEVEMENTS_AUTO_CHECK_ENABLED: bool = True`
   - `ACHIEVEMENTS_SWEEP_ACTIVE_DAYS: int = 7`

4. **Menu-open check** — unchanged; still calls `check_and_unlock_all` on open for instant
   feedback.

## Error handling

- Sweep gated off → no-op. `check_and_unlock_all` already returns `[]` when
  `ACHIEVEMENTS_ENABLED` is false (double safety).
- Per-user exceptions are caught, logged at warning, and the sweep continues.
- Notification failures inside `check_and_unlock_all` are already handled there.

## Testing

- Unit: `get_achievement_sweep_user_ids` returns the union of active/trial-subscription
  users and recently-updated active users, and excludes users with `telegram_id IS NULL`
  and stale inactive users.
- Unit: `_check_achievements` is a no-op when `ACHIEVEMENTS_AUTO_CHECK_ENABLED` is false
  (monkeypatch settings; assert `check_and_unlock_all` not awaited).
- Unit: when enabled, `_check_achievements` calls `check_and_unlock_all` once per candidate
  id (monkeypatch the helper to return a fixed id list and `check_and_unlock_all` to a mock).

## Files touched (anticipated)

- `app/database/crud/achievement.py` — `get_achievement_sweep_user_ids` helper.
- `app/services/monitoring_service.py` — `_check_achievements` + call in `_monitoring_cycle`.
- `app/config.py` — two settings.
- `tests/` — tests for the helper and the sweep gate.
