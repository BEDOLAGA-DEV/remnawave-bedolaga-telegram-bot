# Pre-Release Cross-Cutting Security & Integrity Audit — Design

**Date:** 2026-04-27
**Author:** Claude (in collaboration with project owner)
**Status:** Draft, awaiting user review

## Goal

Sweep the project for cross-cutting bug classes (security, integrity, race
conditions) before the next release, and codify already-fixed regressions as
automated tests. The audit produces concrete findings (real bug, false
positive, accept-with-rationale) and patches the high-severity ones inline.

This is one slice of a larger pre-release audit programme. Tier 1 (money
paths), Tier 2 (auth/user flows), Tier 3 (admin), and Tier 4 (background
services) deep-dives are separate specs. This spec covers the cross-cutting
sweep that applies across all tiers, plus the regression-test harness for
bugs already fixed in prior sessions.

## Non-goals

- New feature work.
- Performance tuning (N+1 noted in passing, only chased if it crosses into a
  correctness issue).
- Frontend UX bugs unrelated to security.
- Load testing or external penetration testing.

## Architecture

The audit runs in three sequential phases, each producing its own report
under `docs/superpowers/audits/`:

```
Phase 1 (sweep)    → grep dangerous patterns, list hits, triage
                     ↓
Phase 2 (deep)     → manual review of three risk classes:
                       (a) money-path race conditions
                       (b) auth bypass / IDOR
                       (c) webhook signature verification (15 providers)
                     ↓
Phase 3 (regress)  → pytest covering already-fixed bugs
                     (WL traffic, achievements multi-sub, surrogate
                      emoji, expired-sub link, branding, …)
```

Each phase is independently consumable: Phase 1 can ship as a punch-list,
Phase 2 produces patches, Phase 3 adds permanent test coverage.

## Components

### Phase 1 — Static-pattern sweep

Patterns to grep across `app/` (Python) and `bedolaga-cabinet/src/`
(TypeScript/React) and `migrations/`:

**Python (backend):**
- Raw SQL injection vectors:
  `text\(.*\{.*\}`, `execute\(.*f['"]`, `execute\(.*format\(`,
  any user-data interpolation into `text()`.
- Surrogate-pair string escapes: `\\ud[89a-f][0-9a-f]{2}\\ud[c-f][0-9a-f]{2}`
  (already caught one such bug in `CONDITION_TYPES`).
- Swallowed exceptions: `except.*:\s*pass\b`,
  `except Exception.*:\s*pass`, broad `except:` blocks.
- Code execution sinks: `eval\(`, `exec\(`, `__import__\(`, `shell=True`,
  `os\.system\(`, `os\.popen\(`.
- Pickle deserialisation: `pickle\.loads`.
- Hardcoded secrets: literal BOT_TOKEN/API_KEY/PASSWORD strings in source
  (token-shaped patterns, not env reads).
- Missing auth on admin endpoints: FastAPI routes whose path contains
  `/admin/` but whose function does not depend on `require_permission(...)`
  or have an `@admin_required` decorator.
- Money-path race conditions: balance/transaction mutations that don't go
  through `lock_user_for_pricing` or a row-level `with_for_update`.
- N+1 in tight loops: missing `selectinload` on FK access inside
  `for x in users` patterns.

**Frontend (React/TS):**
- `dangerouslySetInnerHTML` not wrapped by an approved sanitiser
  (the codebase's `TelegramHtml` and `NewsArticle` use isolated DOMPurify
  instances — those are the allow-list).
- `eval\(`, `new Function\(`.
- `localStorage.setItem` storing tokens/secrets/passwords.
- Anchor `href` from user input without scheme validation (`javascript:`
  XSS).
- `target="_blank"` without `rel="noopener noreferrer"`.

**SQL migrations (`migrations/alembic/versions/`):**
- `ALTER TABLE … ADD COLUMN … NOT NULL` without a server default or backfill
  step.
- `DROP COLUMN`/`DROP TABLE` without an explicit downgrade.
- FK columns without an index.

**Output:** `docs/superpowers/audits/2026-04-27-phase1-sweep.md` — table of
{pattern, file, line, snippet, severity, decision (real / false-positive /
accept-with-rationale), action (quick-fix / queue-phase-2 / accept)}.

### Phase 2 — Targeted deep dive

Three predetermined risk classes. Each gets a dedicated section in the
Phase 2 report with findings and patches.

**(a) Money-path race conditions.** Trace every mutation of
`User.balance_kopeks`, `Transaction`, `Subscription` lifecycle, and
`PromoCode.current_uses`. For each, verify atomicity (single transaction),
locking (row-level lock when reading-then-writing), and idempotency
(payment webhooks must be safe to replay). Files in scope:
`app/services/subscription_purchase_service.py`,
`app/handlers/subscription/tariff_purchase.py`,
`app/handlers/subscription/purchase.py`,
`app/services/payment/*.py`,
`app/database/crud/transaction.py`,
`app/database/crud/promocode.py`,
`app/services/recurrent_payment_service.py`,
`app/services/subscription_renewal_service.py`,
`app/cabinet/routes/withdrawal.py`,
`app/services/referral_service.py`.

**(b) Auth bypass / IDOR.** Enumerate every cabinet route under
`/cabinet/admin/*` and confirm each depends on `require_permission(...)`.
Enumerate every bot admin handler and confirm the `@admin_required`
decorator is present. For every endpoint that takes a resource ID in the
URL (`{review_id}`, `{ticket_id}`, `{subscription_id}`, etc.), verify the
handler asserts `resource.user_id == current_user.id` (or admin override).
JWT verification: confirm algorithm pinning (HS256 only), audience/issuer
checks, refresh-token rotation, and that the signing secret comes from
`CABINET_JWT_SECRET` (not the bot token fallback) in production.

**(c) Webhook signature verification.** 15 incoming webhook receivers
(yookassa, cryptobot, heleket, mulenpay, pal24, platega, freekassa,
kassa_ai, riopay, severpay, paypear, rollypay, aurapay, wata,
cloudpayments) plus the Remnawave webhook. For each: (1) signature/HMAC
verified before any state mutation, (2) replay protection (timestamp or
nonce), (3) IP allow-list where the provider publishes one (yookassa).
Document any provider that doesn't verify and either patch it or accept
with explicit rationale.

**Output:** `docs/superpowers/audits/2026-04-27-phase2-deepdive.md` —
findings by risk class, with severity (critical/high/medium/low),
reproducer steps, and patch links.

### Phase 3 — Regression tests

Codify bugs already fixed in prior sessions as pytest tests under
`tests/regression/`. Each test fails on the pre-fix code and passes on
current `main`. The list, with the file under test:

1. **`test_wl_traffic_trial_to_paid_same_tariff`** —
   `app/database/crud/subscription.py::extend_subscription` must sync
   `wl_traffic_limit_gb` from the tariff even when `tariff_id` doesn't
   change (was gated behind `is_tariff_change or was_expired`).
2. **`test_achievement_multi_sub_period_days`** —
   `app/database/crud/achievement.py::_get_user_stat('subscription_period_days')`
   must fall back to `(end_date - start_date).days` for direct paid
   purchases that skip trial conversion.
3. **`test_achievement_referral_count_paid_only`** — fake unfunded
   referrals must not count toward the Ambassador chain.
4. **`test_achievement_review_left_approved_only`** — pending/rejected
   reviews must not count.
5. **`test_admin_achievements_no_surrogate_escapes`** — module load +
   scan `CONDITION_TYPES`/`REWARD_TYPES` for codepoints in the surrogate
   range U+D800..U+DFFF.
6. **`test_renewal_price_uses_pricing_engine`** —
   `_send_expired_day1_notification` produces a price computed by
   `pricing_engine.calculate_renewal_price`, not the legacy
   `settings.PRICE_30_DAYS` (mock the engine and assert it's called).
7. **`test_review_user_display_anonymized_email`** —
   `format_user_public_display` for a cabinet-only user (no Telegram
   username, no `first_name`, only `email`) returns the `xx***@domain`
   form.

Tests use the existing pytest harness (`pytest`/`pytest-asyncio`) and
re-use existing fixtures where possible. New fixtures live in
`tests/regression/conftest.py`.

**Output:** green test run + commit of tests under `tests/regression/`.

## Data flow

**Phase 1.**
1. Run grep patterns from the catalogue above across the relevant trees.
2. For each hit, classify and record the decision in the Phase 1 report.
3. Quick-fix patches go in immediately as separate small commits;
   queue-for-Phase-2 hits are tagged in the report.
4. Phase 1 closes when every hit has a decision.

**Phase 2.**
1. Read code path-by-path for each risk class.
2. For each finding, write severity, reproducer, and a fix or a defer
   rationale.
3. Critical and high severity findings are patched immediately and
   appended to the regression-test backlog.
4. Medium and low findings either ship as queued issues or are accepted
   with explicit rationale in the report.
5. Phase 2 closes when all three risk classes have a written section.

**Phase 3.**
1. Write each test in the list above.
2. Run `pytest tests/regression/ -v` — must pass on current `main`.
3. Optional verification: temporarily revert one fix at a time, confirm
   the corresponding test fails, restore the fix.
4. Commit `tests/regression/` and update `pytest` configuration if needed
   so the new directory is discovered.

## Error handling

- Grep tool errors (encoding, missing files) — log, skip the file,
  continue.
- Triage uncertainty in Phase 1 — mark `needs-review` and surface to the
  user rather than guessing.
- Phase 2 patch breaks an existing test — do not auto-revert; surface the
  conflict and ask before proceeding.
- Phase 3 test fails on `main` — investigate; either the fix isn't where
  we think it is, or the test is wrong. Do not blanket-pass.
- Container rebuild failure during patching — halt and surface the error;
  do not push partial state.

## Testing

Phase 3 is the explicit test plan. In addition:

- After every Phase 1 quick-fix patch, run the full existing test suite
  (`pytest`) to catch regressions.
- Frontend changes from Phase 1 (e.g. `dangerouslySetInnerHTML` fixes)
  must pass `npm run build` in `bedolaga-cabinet/`.
- Backend changes that touch Docker-baked code (anything under `app/`)
  trigger `docker compose build bot && docker compose up -d bot`, plus a
  log scan for ImportError / startup tracebacks.

## Out of scope (explicit)

- Tier-specific deep audits (each gets its own spec): payment webhooks
  beyond signature verification, subscription pricing engine internals,
  withdrawal abuse scenarios, bot FSM correctness, broadcast service
  integrity, monitoring/scheduler reliability, frontend UX defects.
- Performance work (chunk sizes, query plans, caching) unless a finding
  also affects correctness.
- Penetration testing — recommend an external pass before public release.

## Success criteria

- Three audit reports committed under `docs/superpowers/audits/`.
- Every Phase 1 hit has a recorded decision (no "TBD").
- Every critical and high severity Phase 2 finding has a merged patch or
  an explicit rationale for deferral.
- `pytest tests/regression/` is green and runs in CI/local default.
- A short summary lands in the release notes covering what was scanned,
  what was fixed, and what was deferred.
