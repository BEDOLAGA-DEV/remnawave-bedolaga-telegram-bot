# Pre-Release Cross-Cutting Security Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sweep the codebase for cross-cutting security/integrity bug classes before release. Produce three audit artifacts and a pytest regression suite that codifies bugs already fixed in prior sessions.

**Architecture:** Three sequential phases.
1. **Static sweep** — grep dangerous patterns across `app/`, `bedolaga-cabinet/src/`, `migrations/`. Triage every hit, quick-fix obvious wins, queue the rest for Phase 2.
2. **Deep dive** — manual review of three predetermined risk classes: money-path race conditions, auth bypass / IDOR, webhook signature verification (16 receivers).
3. **Regression tests** — pytest cases for each bug already fixed in prior sessions, committed under `tests/regression/`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 (async), aiogram, pytest + pytest-asyncio, ripgrep, docker compose.

---

## File Structure

Created:
- `docs/superpowers/audits/2026-04-27-phase1-sweep.md` — Phase 1 findings table.
- `docs/superpowers/audits/2026-04-27-phase2-deepdive.md` — Phase 2 deep-dive report.
- `tests/regression/__init__.py` — empty marker.
- `tests/regression/conftest.py` — fixtures for regression tests.
- `tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py`
- `tests/regression/test_achievement_multi_sub_period_days.py`
- `tests/regression/test_achievement_referral_count_paid_only.py`
- `tests/regression/test_achievement_review_left_approved_only.py`
- `tests/regression/test_admin_achievements_no_surrogate_escapes.py`
- `tests/regression/test_renewal_price_uses_pricing_engine.py`
- `tests/regression/test_review_user_display_anonymized_email.py`

Modified inline (paths discovered during Phase 1/2 triage; engineers commit each fix as a separate small commit):
- Files matched by Phase 1 patterns that resolve to real bugs.
- Files cited in Phase 2 critical/high findings.

---

## Phase 1 — Static Sweep

### Task 1.1: Initialize Phase 1 report scaffold

**Files:**
- Create: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Write the report skeleton**

```markdown
# Phase 1 Static Sweep — 2026-04-27

Scope: cross-cutting bug-class grep across `app/`, `bedolaga-cabinet/src/`, `migrations/`.

## Findings

| # | Pattern | File | Line | Snippet | Severity | Decision | Action |
|---|---------|------|------|---------|----------|----------|--------|

(Severity: critical / high / medium / low / info.
 Decision: real-bug / false-positive / accept-with-rationale.
 Action: quick-fix-applied / queue-phase2 / accept.)

## Pattern catalogue

- P1: Raw SQL injection vectors
- P2: Surrogate-pair string escapes
- P3: Swallowed exceptions
- P4: Code execution sinks
- P5: Hardcoded secrets
- P6: Missing auth on admin endpoints
- P7: Money-path race conditions
- P8: dangerouslySetInnerHTML without sanitiser
- P9: eval / new Function / sensitive localStorage
- P10: target="_blank" without rel="noopener noreferrer"
- P11: SQL migration anti-patterns

## Summary

(filled at end of Phase 1)
- Total hits: TBD-fill
- Real bugs (quick-fixed): TBD-fill
- Real bugs (queued for Phase 2): TBD-fill
- False positives: TBD-fill
- Accepted with rationale: TBD-fill
```

- [ ] **Step 2: Commit the scaffold**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md
git commit -m "audit(phase1): scaffold sweep report"
```

### Task 1.2: P1 — Raw SQL injection vectors

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep for f-string interpolation into execute/text**

Run:
```bash
rg -n --type py "execute\(\s*f['\"]" app/
rg -n --type py "execute\(\s*['\"].*\.format\(" app/
rg -n --type py "text\(\s*f['\"]" app/
rg -n --type py "text\([^)]*\{[^}]*\}" app/
```

Expected: list of file:line hits, possibly empty.

- [ ] **Step 2: For each hit, read 5 lines of context**

Run:
```bash
rg -n --type py -B 2 -A 3 "execute\(\s*f['\"]" app/
```

For each hit, classify whether the interpolated value comes from user input (real bug) or from a constant/internal value (false positive).

- [ ] **Step 3: Append rows to Phase 1 report**

For every hit, add a row to the findings table with:
- Pattern: `P1`
- File / line / snippet (1-line excerpt)
- Severity: `critical` if user-controlled, `info` if constant
- Decision: real-bug / false-positive
- Action: `quick-fix-applied` (parametrize the query) or `queue-phase2` if non-trivial

- [ ] **Step 4: Quick-fix obvious cases**

For real-bug hits with a trivial fix, replace `text(f"... {x}")` with parametrised form:

```python
# Before
db.execute(text(f"SELECT * FROM t WHERE id = {x}"))

# After
db.execute(text("SELECT * FROM t WHERE id = :x"), {"x": x})
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md app/
git commit -m "audit(phase1): P1 raw SQL scan + quick fixes"
```

### Task 1.3: P2 — Surrogate-pair string escapes

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep**

Run:
```bash
rg -n --type py "\\\\ud[89a-f][0-9a-f]{2}\\\\ud[c-f][0-9a-f]{2}" app/
```

Expected: zero hits (the one in `app/handlers/admin/achievements.py::CONDITION_TYPES` was already fixed in a prior session). Any new hits are real bugs.

- [ ] **Step 2: For each hit (if any), confirm via Python module load**

Run:
```bash
docker exec remnawave_bot python -c "
from <module> import <DICT_NAME>
for k, v in <DICT_NAME>.items():
    for ch in v:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            print('BAD', k, hex(ord(ch))); break
"
```

- [ ] **Step 3: Quick-fix by rewriting escapes as `\U0001fXXX`**

For every offending value, compute the supplementary codepoint from the surrogate pair using:
```
codepoint = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
```
Replace `'\udXYZ\udABC'` with `'\U000XXXXX'` literally.

- [ ] **Step 4: Append rows + commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md app/
git commit -m "audit(phase1): P2 surrogate-pair scan"
```

### Task 1.4: P3 — Swallowed exceptions

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep for bare `except: pass`**

Run:
```bash
rg -n --type py "except[^:]*:\s*pass\b" app/
rg -n --type py "except[^:]*:\s*continue\b" app/
rg -n --type py "^\s*except\s*:" app/
```

- [ ] **Step 2: Triage each hit**

For each, read 5 lines context. Classify:
- `accept-with-rationale` if catching is intentional (e.g. bot send to user that blocked us, telemetry write that fails) — note rationale in report.
- `real-bug` if the swallowed error masks a logic failure (e.g. silently dropping a payment).

- [ ] **Step 3: For real-bug hits, replace `pass` with at minimum a `logger.warning(...)`**

```python
# Before
try:
    do_thing()
except Exception:
    pass

# After
try:
    do_thing()
except Exception as exc:
    logger.warning('do_thing failed', exc_info=exc)
```

- [ ] **Step 4: Append rows + commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md app/
git commit -m "audit(phase1): P3 swallowed-exception scan"
```

### Task 1.5: P4 — Code execution sinks

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep for dangerous calls**

Run:
```bash
rg -n --type py "\beval\(" app/
rg -n --type py "\bexec\(" app/
rg -n --type py "__import__\(" app/
rg -n --type py "shell\s*=\s*True" app/
rg -n --type py "os\.system\(" app/
rg -n --type py "os\.popen\(" app/
```

- [ ] **Step 2: Triage**

Any hit on user-controllable input is `critical`. Any compile-time constant is `accept`.

- [ ] **Step 3: Quick-fix critical hits**

Replace `os.system(cmd)` → `subprocess.run([…], check=True)` without `shell=True`. Replace `eval(s)` for JSON-shaped data → `json.loads(s)`.

- [ ] **Step 4: Append rows + commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md app/
git commit -m "audit(phase1): P4 code-execution-sink scan"
```

### Task 1.6: P5 — Hardcoded secrets

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep for token-shaped literals**

Run:
```bash
rg -n --type py -e "[0-9]{6,}:[A-Za-z0-9_-]{30,}" app/  # Telegram token shape
rg -n --type py -e "AKIA[A-Z0-9]{16}" app/              # AWS access key
rg -n --type py -e "sk_live_[A-Za-z0-9]+" app/          # Stripe live key
rg -n --type py -e "BOT_TOKEN\s*=\s*['\"][^'\"]" app/   # literal assignment
```

- [ ] **Step 2: Triage**

Any literal string that looks like a real credential is `critical`. Test fixtures and `.env.example` are `accept`.

- [ ] **Step 3: For each real hit, move to env var and rotate**

Replace literal with `settings.<KEY>` or `os.environ.get(...)`. Note in report that the engineer must also rotate the leaked credential out-of-band.

- [ ] **Step 4: Append rows + commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md app/
git commit -m "audit(phase1): P5 hardcoded-secret scan"
```

### Task 1.7: P6 — Missing admin auth on cabinet routes

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: List every cabinet admin route**

Run:
```bash
rg -n --type py "@router\.(get|post|put|delete|patch)\(" app/cabinet/routes/admin_*.py
```

- [ ] **Step 2: For each route, read its function signature and check for `Depends(require_permission(`**

Run:
```bash
rg -n --type py -A 8 "@router\.(get|post|put|delete|patch)\(" app/cabinet/routes/admin_*.py | grep -E "@router|require_permission|def " | head -200
```

- [ ] **Step 3: Note any function lacking `require_permission(` in its parameter list**

Append to Phase 1 report. Classify as `critical` (broken access control).

- [ ] **Step 4: Quick-fix obvious gaps**

Add `admin: User = Depends(require_permission('<scope>:<action>'))` where missing. Use closest-matching scope name from existing routes.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md app/cabinet/routes/
git commit -m "audit(phase1): P6 cabinet admin auth scan"
```

### Task 1.8: P7 — Money-path race-condition shape

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Find balance mutations**

Run:
```bash
rg -n --type py "balance_kopeks\s*[+\-]=" app/ | grep -v test_
rg -n --type py "current_uses\s*[+\-]=" app/ | grep -v test_
```

- [ ] **Step 2: For each, check whether the read uses `lock_user_for_pricing` or `with_for_update()`**

Run:
```bash
rg -n --type py -B 6 "balance_kopeks\s*[+\-]=" app/ | grep -E "lock_user_for_pricing|with_for_update|balance_kopeks"
```

- [ ] **Step 3: Note unlocked mutations as `queue-phase2`**

Don't quick-fix here — locking changes need careful Phase 2 review for transaction boundaries. Just enumerate.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md
git commit -m "audit(phase1): P7 money-path race scan"
```

### Task 1.9: P8 — dangerouslySetInnerHTML without sanitiser

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: List all uses**

Run:
```bash
rg -n "dangerouslySetInnerHTML" bedolaga-cabinet/src/
```

- [ ] **Step 2: For each, check whether the file imports an approved sanitiser**

Approved sanitisers in this codebase:
- `TelegramHtml` component (`src/components/common/TelegramHtml.tsx`)
- `NewsArticle` page's local DOMPurify instance
- `Info` page's local DOMPurify instance
- `HelpArticle` page's local DOMPurify instance

For each `dangerouslySetInnerHTML` hit, the surrounding component must call `DOMPurify.sanitize(...)` on the input or import one of the approved components.

- [ ] **Step 3: Triage**

Any direct `__html: <user_data>` without sanitiser is `critical`. Static or sanitised input is `accept`.

- [ ] **Step 4: Quick-fix by routing through `TelegramHtml`**

```tsx
// Before
<div dangerouslySetInnerHTML={{ __html: data.text }} />

// After
import { TelegramHtml } from '@/components/common/TelegramHtml';
<TelegramHtml text={data.text} />
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md bedolaga-cabinet/src/
git commit -m "audit(phase1): P8 dangerouslySetInnerHTML scan"
```

### Task 1.10: P9 — eval / Function / sensitive localStorage

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep**

Run:
```bash
rg -n -tts "\beval\(" bedolaga-cabinet/src/
rg -n -tts "new Function\(" bedolaga-cabinet/src/
rg -n -tts "localStorage\.setItem\(['\"](token|secret|password|jwt|refresh)" bedolaga-cabinet/src/
```

- [ ] **Step 2: Triage**

`eval`/`new Function` on user input → `critical`. localStorage of long-lived tokens → `high` (recommend httpOnly cookie or short-lived in-memory).

- [ ] **Step 3: Append rows + commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md
git commit -m "audit(phase1): P9 eval/Function/localStorage scan"
```

### Task 1.11: P10 — target="_blank" without rel="noopener noreferrer"

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep**

Run:
```bash
rg -n -tts "target\s*=\s*['\"]_blank['\"]" bedolaga-cabinet/src/ -B 1 -A 2 \
  | grep -v "noopener" | grep -v "noreferrer"
```

- [ ] **Step 2: For each hit lacking the rel attribute, quick-fix**

Add `rel="noopener noreferrer"` to the `<a>` tag.

- [ ] **Step 3: Append rows + commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md bedolaga-cabinet/src/
git commit -m "audit(phase1): P10 target=_blank scan"
```

### Task 1.12: P11 — SQL migration anti-patterns

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Run grep**

Run:
```bash
rg -n "ADD COLUMN.*NOT NULL" migrations/alembic/versions/ | grep -vi "default"
rg -n "DROP COLUMN|DROP TABLE" migrations/alembic/versions/
rg -n "ForeignKey" migrations/alembic/versions/ | head -50
```

- [ ] **Step 2: Triage**

`ADD COLUMN NOT NULL` without server default or backfill → `high` (will break upgrade on non-empty tables). `DROP COLUMN/TABLE` without downgrade → `medium`. Missing index on FK → `medium` (perf, but flag).

- [ ] **Step 3: Note in report; do NOT quick-fix migrations (already-shipped migrations stay immutable)**

Queue future migration to add backfill or index.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md
git commit -m "audit(phase1): P11 SQL migration scan"
```

### Task 1.13: Phase 1 close — fill summary, run test suite

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase1-sweep.md`

- [ ] **Step 1: Compute totals and fill the Summary section**

Replace the `TBD-fill` placeholders with actual counts: total hits, real-bugs-quick-fixed, real-bugs-queued, false-positives, accepted.

- [ ] **Step 2: Run the existing test suite to catch regressions from quick-fixes**

Run:
```bash
docker exec remnawave_bot pytest -x --tb=short 2>&1 | tail -30
```

Expected: all green. If any failure trace points to a Phase 1 quick-fix, revert that fix and re-classify the hit as `queue-phase2`.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase1-sweep.md
git commit -m "audit(phase1): finalize sweep report"
```

---

## Phase 2 — Targeted Deep Dive

### Task 2.1: Initialize Phase 2 report scaffold

**Files:**
- Create: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: Write the skeleton**

```markdown
# Phase 2 Deep Dive — 2026-04-27

Three risk classes:
- (a) Money-path race conditions
- (b) Auth bypass / IDOR
- (c) Webhook signature verification

## (a) Money-path race conditions

### Mutations enumerated
(filled in Task 2.2)

### Findings

| # | File | Line | Description | Severity | Reproducer | Patch / Defer |
|---|------|------|-------------|----------|-----------|---------------|

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
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md
git commit -m "audit(phase2): scaffold deep-dive report"
```

### Task 2.2: Money-path race — enumerate every mutation

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: Find every place balance is written**

Run:
```bash
rg -n --type py "balance_kopeks\s*[+\-]=" app/
rg -n --type py "balance_kopeks\s*=\s*" app/ | grep -v "balance_kopeks =="
rg -n --type py "add_user_balance\(" app/
rg -n --type py "subtract_user_balance\(" app/
```

- [ ] **Step 2: Find every place a Transaction row is written**

Run:
```bash
rg -n --type py "Transaction\(" app/
rg -n --type py "create_transaction\(" app/
```

- [ ] **Step 3: Find every place a PromoCode counter is incremented**

Run:
```bash
rg -n --type py "current_uses\s*[+\-]=" app/
```

- [ ] **Step 4: Append the enumerated locations under "Mutations enumerated" in the Phase 2 report (one bullet per file:line)**

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md
git commit -m "audit(phase2a): enumerate money-path mutations"
```

### Task 2.3: Money-path race — verify each mutation

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: For each mutation from Task 2.2, read the surrounding 30 lines and check three properties**

Properties:
- **Atomicity:** all related writes happen in the same transaction (no `await db.commit()` between balance write and Transaction insert).
- **Locking:** the read of `balance_kopeks` (or `current_uses`) happens with `with_for_update()` or via `lock_user_for_pricing(...)` so a concurrent reader can't race.
- **Idempotency:** if the entry point is a webhook, the handler checks for already-processed payment ID before crediting.

- [ ] **Step 2: For each mutation that fails one of the three properties, write a finding row**

Severity:
- `critical` if a real-money double-spend or double-credit is reachable.
- `high` if reachable only under specific edge conditions.
- `medium` otherwise.

Reproducer: 1-3 line race scenario (e.g. "two concurrent /balance/topup webhooks for the same payment_id").

- [ ] **Step 3: For critical/high findings, write a patch**

Common patches:
- Wrap the read+write in `lock_user_for_pricing(db, user_id)`.
- Add a uniqueness check against `Transaction.external_payment_id` before inserting.
- Wrap multi-step state mutation in a single `async with db.begin():` block.

- [ ] **Step 4: Apply the patches, run the existing test suite**

Run:
```bash
docker exec remnawave_bot pytest -x --tb=short 2>&1 | tail -30
```

Expected: all green. If a money-path test breaks, the patch likely changed transaction semantics — surface to user before continuing.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md app/
git commit -m "audit(phase2a): money-path race findings + patches"
```

### Task 2.4: Auth — enumerate cabinet admin routes

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: List every admin route file**

Run:
```bash
ls app/cabinet/routes/admin_*.py
```

- [ ] **Step 2: For each file, list (path, method, function, permission_scope_or_None)**

Run:
```bash
rg -n --type py "@router\.(get|post|put|delete|patch)\(" app/cabinet/routes/admin_*.py
```

For each route, also read the function signature to extract the `require_permission(...)` argument or note `MISSING`.

- [ ] **Step 3: Append a table in the report under "Cabinet admin routes audited"**

| File | Route | Method | Permission | Status |
|------|-------|--------|------------|--------|
| ... | ... | ... | `reviews:approve` or `MISSING` | OK / GAP |

- [ ] **Step 4: For every `MISSING` row, write a finding (severity `critical`) and patch**

Patch template:
```python
async def some_admin_route(
    payload: SomeRequest,
    admin: User = Depends(require_permission('<scope>:<action>')),
    db: AsyncSession = Depends(get_cabinet_db),
):
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md app/cabinet/routes/
git commit -m "audit(phase2b): cabinet admin route enumeration + auth patches"
```

### Task 2.5: Auth — enumerate bot admin handlers

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: Find every handler file under `app/handlers/admin/`**

Run:
```bash
ls app/handlers/admin/
```

- [ ] **Step 2: For each, list functions and check for `@admin_required` decorator**

Run:
```bash
rg -n --type py "^async def |^def " app/handlers/admin/
rg -n --type py "@admin_required" app/handlers/admin/
```

Diff the two: every async handler under `app/handlers/admin/` should have `@admin_required` directly above it. List exceptions (helpers may not need it).

- [ ] **Step 3: For genuine gaps, add the decorator**

```python
from app.utils.decorators import admin_required, error_handler

@admin_required
@error_handler
async def some_admin_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    ...
```

- [ ] **Step 4: Append the table + findings to the report, commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md app/handlers/admin/
git commit -m "audit(phase2b): bot admin handler decorator audit"
```

### Task 2.6: IDOR — endpoints with URL ID parameters

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: List every cabinet route with a path parameter**

Run:
```bash
rg -n --type py "@router\.(get|post|put|delete|patch)\([^)]*\{" app/cabinet/routes/
```

- [ ] **Step 2: For each, check whether the handler asserts ownership**

Read each function (~30 lines context). The handler must either:
- Compare `resource.user_id == current_user.id` before any read/write, OR
- Be admin-gated by `require_permission(...)`.

Examples of the pattern (from existing code):
```python
review = await get_review_by_id(db, review_id)
if review.user_id != user.id:
    raise HTTPException(status_code=404)
```

- [ ] **Step 3: For every endpoint missing the check, write a finding**

Severity `critical` if PII leak or state mutation. `high` if read-only with non-sensitive data.

- [ ] **Step 4: Patch by adding the ownership check**

- [ ] **Step 5: Append findings + commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md app/cabinet/routes/
git commit -m "audit(phase2b): IDOR audit + ownership patches"
```

### Task 2.7: JWT verification deep dive

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: Locate the JWT decoder**

Run:
```bash
rg -n --type py "jwt\.decode\(" app/
rg -n --type py "jwt\.encode\(" app/
```

- [ ] **Step 2: For the decoder, verify five properties**

- Algorithm pinned (`algorithms=['HS256']`), not derived from header.
- `audience=` and `issuer=` checked or accepted absence is documented.
- Expiration claim (`exp`) is enforced (default for PyJWT; document if disabled).
- Signing secret comes from `settings.CABINET_JWT_SECRET`. Note in the report whether the codebase falls back to `BOT_TOKEN` and under what condition (the prior session noted a fallback warning).
- Refresh tokens rotated on use (the issued refresh token is invalidated when a new pair is minted).

- [ ] **Step 3: Append findings to the report**

For each violated property, write severity (`critical` for algorithm-confusion or missing exp; `high` for missing rotation; `medium` for fallback secret without prod warning).

- [ ] **Step 4: Patch where straightforward (algorithm pinning, exp check)**

Defer refresh-token rotation if it requires schema work; queue as a separate task.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md app/
git commit -m "audit(phase2b): JWT verification deep dive"
```

### Task 2.8: Webhook signatures — enumerate

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: List all webhook receivers**

Run:
```bash
rg -n --type py -e "WEBHOOK_PATH\s*=\s*['\"]" app/config.py
rg -n --type py "@app\.post|@router\.post" app/external/ app/services/payment/
```

Expected providers (16): yookassa, cryptobot, heleket, mulenpay, pal24, platega, freekassa, kassa_ai, riopay, severpay, paypear, rollypay, aurapay, wata, cloudpayments, remnawave.

- [ ] **Step 2: For each, find the handler entrypoint**

Search by path string:
```bash
rg -n --type py "yookassa-webhook|cryptobot-webhook|heleket-webhook" app/
```

- [ ] **Step 3: Append a row per provider in the Phase 2 webhook table**

Columns: provider, file, verifies signature?, replay protection?, IP allow-list?, severity, patch/defer.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md
git commit -m "audit(phase2c): webhook receivers enumerated"
```

### Task 2.9: Webhook signatures — verify each

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: For each webhook handler, read the function and look for HMAC verification**

For each provider, the handler must:
- Compute HMAC over the raw request body using the secret from settings.
- Compare with the signature from a header (`X-Signature`, `Sign`, etc., depending on provider).
- Reject before any state mutation if signature mismatches.

Look for one of these patterns in each handler:
```python
hmac.compare_digest(expected, received)
hmac.new(secret, body, hashlib.sha256).hexdigest()
```

- [ ] **Step 2: For each gap, fill the row**

Mark `critical` for any provider that doesn't verify the signature at all (anyone can credit a balance via a fake webhook). Mark `high` for missing replay protection on a provider that has signature but no nonce/timestamp.

- [ ] **Step 3: Patch critical gaps**

For a provider missing verification:
```python
import hmac, hashlib
def _verify_<provider>(body: bytes, signature: str) -> bool:
    secret = settings.<PROVIDER>_WEBHOOK_SECRET.encode()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Reject before processing if `_verify_*` returns False.

- [ ] **Step 4: Run existing test suite (some webhook tests live under `tests/external/`)**

```bash
docker exec remnawave_bot pytest tests/external/ -x --tb=short 2>&1 | tail -30
```

Expected: all green. If a test breaks because it sent a fake webhook without signature, update the test to construct a valid signature.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md app/external/ app/services/payment/
git commit -m "audit(phase2c): webhook signature findings + critical patches"
```

### Task 2.10: Phase 2 close — fill summary

**Files:**
- Modify: `docs/superpowers/audits/2026-04-27-phase2-deepdive.md`

- [ ] **Step 1: Fill the Summary section**

Counts by risk class and severity. List every deferred finding with rationale.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/audits/2026-04-27-phase2-deepdive.md
git commit -m "audit(phase2): finalize deep-dive report"
```

---

## Phase 3 — Regression Tests

### Task 3.0: Set up `tests/regression/` package

**Files:**
- Create: `tests/regression/__init__.py`
- Create: `tests/regression/conftest.py`

- [ ] **Step 1: Create the package marker**

```python
# tests/regression/__init__.py
"""Regression tests for bugs already fixed in prior sessions.

Each test in this package corresponds to one specific bug. The tests
should pass on `main` (the fix is in place) and fail when the fix is
reverted. Use `git revert` (then re-restore) on the fixing commit to
verify locally before relying on a green run."""
```

- [ ] **Step 2: Create the conftest with pytest-asyncio mode**

```python
# tests/regression/conftest.py
"""Shared fixtures for regression tests."""
import pytest


pytest_plugins = ['pytest_asyncio']
```

- [ ] **Step 3: Verify pytest discovers the new directory**

Run:
```bash
docker exec remnawave_bot pytest tests/regression/ --collect-only 2>&1 | tail -10
```

Expected: `0 tests collected` (no test files yet) — but no collection errors.

- [ ] **Step 4: Commit**

```bash
git add tests/regression/
git commit -m "test(regression): add regression test package scaffold"
```

### Task 3.1: `test_wl_traffic_trial_to_paid_same_tariff`

**Files:**
- Create: `tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py
"""Regression: extend_subscription must sync wl_traffic_limit_gb on every
extend, not only on tariff change / expired sub.

Before fix: the WL update was gated behind `is_tariff_change or was_expired`,
so a trial→paid conversion on the same tariff_id (TRIAL_TARIFF_ID) left the
subscription with the trial's default WL=5 even though the tariff says 15.
"""
import inspect
from app.database.crud import subscription as sub_crud


def test_extend_subscription_wl_update_not_gated_by_tariff_change():
    """Source-level guard: the WL-limit assignment must NOT be inside an
    `if … (is_tariff_change or was_expired):` block any longer.

    We check the function source for the post-fix invariant: the assignment
    `subscription.wl_traffic_limit_gb = wl_traffic_limit_gb` must appear
    inside `if wl_traffic_limit_gb is not None:` and the counter reset
    (`subscription.wl_traffic_used_gb = 0.0`) must be in a separately-gated
    branch.
    """
    src = inspect.getsource(sub_crud.extend_subscription)

    # Post-fix: limit update happens whenever wl_traffic_limit_gb is provided.
    assert 'if wl_traffic_limit_gb is not None:' in src, (
        'Expected unconditional WL limit sync block — pre-fix gate may be back.'
    )

    # Post-fix: the counter reset is in a separate `is_tariff_change or was_expired`
    # block, not the same one as the limit assignment.
    limit_assign_idx = src.index('subscription.wl_traffic_limit_gb = wl_traffic_limit_gb')
    counters_reset_idx = src.index('subscription.wl_traffic_used_gb = 0.0')
    gate_idx = src.index('is_tariff_change or was_expired')

    # The limit assignment should appear BEFORE the gated reset block.
    assert limit_assign_idx < gate_idx < counters_reset_idx, (
        'Expected order: assign limit → gate check → reset counters. '
        'Pre-fix code had assignment INSIDE the gate.'
    )
```

- [ ] **Step 2: Run the test (should pass on `main` because the fix is in)**

Run:
```bash
docker exec remnawave_bot pytest tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/regression/test_wl_traffic_trial_to_paid_same_tariff.py
git commit -m "test(regression): WL-traffic trial→paid same-tariff sync"
```

### Task 3.2: `test_achievement_multi_sub_period_days`

**Files:**
- Create: `tests/regression/test_achievement_multi_sub_period_days.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/regression/test_achievement_multi_sub_period_days.py
"""Regression: subscription_period_days achievement must consider both
SubscriptionConversion.first_paid_period_days AND non-trial subscription
span (end_date - start_date), not only the conversion-table value.
"""
import inspect
from app.database.crud import achievement as ach_crud


def test_subscription_period_days_uses_two_sources():
    src = inspect.getsource(ach_crud._get_user_stat)

    # Conversion source.
    assert 'SubscriptionConversion.first_paid_period_days' in src, (
        'Conversion-row branch missing.'
    )
    # Direct-sub span source.
    assert 'Subscription.end_date' in src and 'Subscription.start_date' in src, (
        'Subscription-span fallback branch missing.'
    )
    # max() of the two sources.
    assert 'max(from_conversion, from_span)' in src, (
        'Final max() over both sources missing — fallback may have been removed.'
    )
```

- [ ] **Step 2: Run + commit**

```bash
docker exec remnawave_bot pytest tests/regression/test_achievement_multi_sub_period_days.py -v
git add tests/regression/test_achievement_multi_sub_period_days.py
git commit -m "test(regression): achievement multi-sub period days fallback"
```

### Task 3.3: `test_achievement_referral_count_paid_only`

**Files:**
- Create: `tests/regression/test_achievement_referral_count_paid_only.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/regression/test_achievement_referral_count_paid_only.py
"""Regression: referral_count condition must count only paid referrals.

Before fix: 25 fake unfunded sign-ups unlocked the Ambassador badge.
After fix: the count is gated by `User.id IN (paid deposit users)`.
"""
import inspect
from app.database.crud import achievement as ach_crud


def test_referral_count_filters_to_paid_users():
    src = inspect.getsource(ach_crud._get_user_stat)

    # Subquery for paid users must be present.
    assert 'paid_refs_subq' in src, 'paid-refs subquery missing'
    # The subquery filters by completed deposits.
    assert 'TransactionType.DEPOSIT.value' in src, (
        'paid-refs subquery should filter by DEPOSIT type'
    )
    assert 'is_completed.is_(True)' in src, (
        'paid-refs subquery should filter by completed transactions'
    )
    # The outer query AND-joins User.id with the subquery.
    assert 'User.id.in_(select(paid_refs_subq.c.user_id))' in src, (
        'Outer referral_count query must filter by paid-refs subquery'
    )
```

- [ ] **Step 2: Run + commit**

```bash
docker exec remnawave_bot pytest tests/regression/test_achievement_referral_count_paid_only.py -v
git add tests/regression/test_achievement_referral_count_paid_only.py
git commit -m "test(regression): referral_count paid-only filter"
```

### Task 3.4: `test_achievement_review_left_approved_only`

**Files:**
- Create: `tests/regression/test_achievement_review_left_approved_only.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/regression/test_achievement_review_left_approved_only.py
"""Regression: review_left achievement must count only approved reviews.

Before fix: a pending or rejected review still unlocked the badge, which
let users farm by submitting throwaway reviews.
"""
import inspect
from app.database.crud import achievement as ach_crud


def test_review_left_filters_by_is_approved():
    src = inspect.getsource(ach_crud._get_user_stat)

    # The review_left branch must filter by UserReview.is_approved.is_(True).
    branch_start = src.index("condition_type == 'review_left'")
    branch = src[branch_start:branch_start + 600]
    assert 'UserReview.is_approved.is_(True)' in branch, (
        'review_left branch must filter by is_approved'
    )
```

- [ ] **Step 2: Run + commit**

```bash
docker exec remnawave_bot pytest tests/regression/test_achievement_review_left_approved_only.py -v
git add tests/regression/test_achievement_review_left_approved_only.py
git commit -m "test(regression): review_left approved-only filter"
```

### Task 3.5: `test_admin_achievements_no_surrogate_escapes`

**Files:**
- Create: `tests/regression/test_admin_achievements_no_surrogate_escapes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/regression/test_admin_achievements_no_surrogate_escapes.py
"""Regression: admin labels must not contain UTF-16 lone surrogates.

Before fix: emojis in CONDITION_TYPES were stored as surrogate-pair escapes
('\\ud83c\\udf9f'), which Python parses but `urllib.parse.quote(...,
encoding='utf-8')` rejects with `UnicodeEncodeError: surrogates not allowed`.
That crashed `callback.message.edit_text(...)` whenever the dict was rendered
into a Telegram form-urlencoded body.
"""
from app.handlers.admin.achievements import CONDITION_TYPES, REWARD_TYPES


def _scan(d, name):
    for key, value in d.items():
        for ch in value:
            cp = ord(ch)
            if 0xD800 <= cp <= 0xDFFF:
                raise AssertionError(
                    f'{name}[{key!r}] contains surrogate U+{cp:04X}: {value!r}'
                )


def test_condition_types_no_surrogates():
    _scan(CONDITION_TYPES, 'CONDITION_TYPES')


def test_reward_types_no_surrogates():
    _scan(REWARD_TYPES, 'REWARD_TYPES')


def test_condition_types_encode_to_utf8():
    """Defensive: every value must encode cleanly to UTF-8 without errors."""
    for key, value in CONDITION_TYPES.items():
        try:
            value.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise AssertionError(f'CONDITION_TYPES[{key!r}] fails utf-8: {exc}')
```

- [ ] **Step 2: Run + commit**

```bash
docker exec remnawave_bot pytest tests/regression/test_admin_achievements_no_surrogate_escapes.py -v
git add tests/regression/test_admin_achievements_no_surrogate_escapes.py
git commit -m "test(regression): admin label no-surrogate guard"
```

### Task 3.6: `test_renewal_price_uses_pricing_engine`

**Files:**
- Create: `tests/regression/test_renewal_price_uses_pricing_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/regression/test_renewal_price_uses_pricing_engine.py
"""Regression: expired-subscription day-1 notification must compute the
renewal price via PricingEngine, not from the legacy global PRICE_30_DAYS.

Before fix: the message hardcoded `settings.PRICE_30_DAYS`, which under
SALES_MODE=tariffs is a misleading classic-mode placeholder. Users with a
real cheaper tariff (40₽ minimum, 33₽ with promo-group discount) saw the
notification claim 100₽.
"""
import inspect
from app.services import monitoring_service


def test_send_expired_day1_notification_uses_pricing_engine():
    src = inspect.getsource(monitoring_service.MonitoringService._send_expired_day1_notification)

    # Post-fix: pricing_engine.calculate_renewal_price must be called.
    assert 'pricing_engine.calculate_renewal_price' in src, (
        'Expected pricing_engine to be called for renewal price calc'
    )

    # Per-tariff shortest-period lookup must be present.
    assert 'tariff.get_shortest_period()' in src, (
        'Expected per-tariff shortest-period lookup'
    )

    # The function must take a `db` parameter so the engine has a session.
    sig = inspect.signature(monitoring_service.MonitoringService._send_expired_day1_notification)
    assert 'db' in sig.parameters, (
        'Function signature must include `db` for PricingEngine access'
    )
```

- [ ] **Step 2: Run + commit**

```bash
docker exec remnawave_bot pytest tests/regression/test_renewal_price_uses_pricing_engine.py -v
git add tests/regression/test_renewal_price_uses_pricing_engine.py
git commit -m "test(regression): renewal price uses pricing_engine"
```

### Task 3.7: `test_review_user_display_anonymized_email`

**Files:**
- Create: `tests/regression/test_review_user_display_anonymized_email.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/regression/test_review_user_display_anonymized_email.py
"""Regression: format_user_public_display must return an anonymized email
for cabinet-only users (no Telegram username, no first_name).

Before fix: such users showed as a generic 'Пользователь' in the review
channel post, with no identifier at all.
"""
from types import SimpleNamespace
from app.utils.user_utils import format_user_public_display, _anonymize_email


def test_telegram_user_uses_username():
    user = SimpleNamespace(
        id=1, username='alice', first_name='Alice', email=None,
    )
    assert format_user_public_display(user) == '@alice'


def test_telegram_user_no_username_uses_first_name():
    user = SimpleNamespace(
        id=1, username=None, first_name='Alice', email=None,
    )
    assert format_user_public_display(user) == 'Alice'


def test_cabinet_only_user_uses_anonymized_email():
    user = SimpleNamespace(
        id=42, username=None, first_name=None, email='mama05693@gmail.com',
    )
    result = format_user_public_display(user)
    assert result == 'ma***@gmail.com', f'expected anonymized email, got {result!r}'


def test_user_with_neither_falls_back_to_id():
    user = SimpleNamespace(
        id=42, username=None, first_name=None, email=None,
    )
    assert format_user_public_display(user) == 'Пользователь #42'


def test_anonymize_email_short_local_part():
    assert _anonymize_email('a@b.co') == 'a***@b.co'


def test_anonymize_email_invalid_returns_empty():
    assert _anonymize_email('') == ''
    assert _anonymize_email('no-at-sign') == ''
    assert _anonymize_email('@nodomain') == ''
```

- [ ] **Step 2: Run + commit**

```bash
docker exec remnawave_bot pytest tests/regression/test_review_user_display_anonymized_email.py -v
git add tests/regression/test_review_user_display_anonymized_email.py
git commit -m "test(regression): user_public_display anonymizes email"
```

### Task 3.8: Run the entire regression suite, then the full test suite

**Files:**
- (none — verification only)

- [ ] **Step 1: Run regression suite**

```bash
docker exec remnawave_bot pytest tests/regression/ -v 2>&1 | tail -30
```

Expected: 7 test modules collected, all pass.

- [ ] **Step 2: Run the full project test suite**

```bash
docker exec remnawave_bot pytest -x --tb=short 2>&1 | tail -50
```

Expected: all green (no regressions introduced by Phase 1/Phase 2 patches that we missed).

- [ ] **Step 3: Optional — verify revert/restore loop for one test**

Pick one fix (e.g. the admin surrogates one) and locally:
```bash
git log --oneline -- app/handlers/admin/achievements.py | head -10
# revert the surrogate-fix commit
git revert --no-commit <fix_sha>
docker exec remnawave_bot pytest tests/regression/test_admin_achievements_no_surrogate_escapes.py -v
# expected: FAIL with surrogate detected
git checkout HEAD -- app/handlers/admin/achievements.py
docker exec remnawave_bot pytest tests/regression/test_admin_achievements_no_surrogate_escapes.py -v
# expected: PASS
```

Skip this step if the verification feels redundant — `git log` already shows the chain.

- [ ] **Step 4: Final commit**

If anything was modified during this task (e.g. discovering a test path that needs adjustment), commit it. Otherwise nothing to do.

---

## Wrap-up

After all phases complete:

- [ ] **Update release notes**

Add a short note covering: which patterns were swept, how many real bugs were quick-fixed, how many critical/high deep-dive findings were patched, regression-test count.

- [ ] **Final commit**

```bash
git add CHANGELOG.md   # or wherever release notes live
git commit -m "docs: pre-release security audit summary"
```

The audit is complete when all three reports are committed and `pytest tests/regression/` is green.

---

## Self-Review

**Spec coverage check:**
- ✅ Phase 1 sweep — Tasks 1.1 through 1.13 cover all 11 patterns from the spec.
- ✅ Phase 2 deep dive — Tasks 2.1 through 2.10 cover all three risk classes (money path, auth/IDOR/JWT, webhook signatures across 16 receivers).
- ✅ Phase 3 regression tests — Tasks 3.0 through 3.8 cover all seven tests enumerated in the spec.
- ✅ Reports under `docs/superpowers/audits/` — both filenames match spec.
- ✅ Tests under `tests/regression/` — directory matches spec.
- ✅ Success criteria addressed (every hit decided, critical/high patched, regression suite green, release-notes summary).

**Placeholder scan:**
- The Phase 1 / Phase 2 report scaffolds intentionally contain `(filled in Task X.Y)` markers — these are pointers within the plan, not unresolved work. Acceptable.
- No "TBD" / "TODO" / "implement later" blockers in plan steps.

**Type / signature consistency:**
- `extend_subscription` referenced consistently as `app.database.crud.subscription.extend_subscription`.
- `_get_user_stat` referenced consistently as `app.database.crud.achievement._get_user_stat`.
- `format_user_public_display` and `_anonymize_email` consistent with `app/utils/user_utils.py` exports.
- `MonitoringService._send_expired_day1_notification` matches the prior session's edit.
- `CONDITION_TYPES` / `REWARD_TYPES` confirmed module-level dicts.

No issues found.
