# BEDOLAGA-PANEL-SYNC-ATOMICITY-SPEC-R1 — Independent Review Result

- **task_id:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-SPEC-R1`
- **reviewed_head:** `fea9c32e12b9edb01939530b2cdda4e50f74b95e`
- **status:** completed
- **decision:** `CHANGES_REQUIRED`
- **timestamp_utc:** `2026-08-02T19:23:29Z`
- **ledger_path:** `docs/superpowers/ledgers/bedolaga-panel-sync-atomicity-spec-r1-chewbacca.md`

## Findings

### Critical

- None.

### Important

1. **The required inventory boundary is internally inconsistent and can omit panel-relevant admin mutations.** The Objective requires blocking synchronization for *every* admin mutation whose state is panel-relevant (spec lines 16-20), and Caller classification likewise says every panel-relevant admin route is mandatory (lines 88-99). However, Scope narrows the inventory to callers of `_sync_subscription_to_panel` (lines 22-35), while Acceptance Criteria and Verification use the undefined phrases “complete caller inventory” and “all mandatory callers” (lines 117-140). Current code proves those sets differ: `update_user_subscription` action `reset` calls `reset_subscription_with_panel`, not `_sync_subscription_to_panel` (`app/cabinet/routes/admin_users.py:1569-1589`); that service swallows panel-disable failure and commits the reset (`app/services/subscription_service.py:1318-1357`). Bulk subscription deletion also invokes the panel directly, swallows failure, commits deletion, and returns success (`app/cabinet/routes/admin_bulk_actions.py:494-590`). **Required change:** define the inventory as all admin mutations that create/update/disable/delete panel-relevant subscription state, regardless of helper/service used; explicitly say whether reset/delete/direct-service paths are included; and require an enumerated route/action inventory plus coverage proving no panel-relevant admin mutation is absent. With R1's empty best-effort list, any included direct-service path must satisfy the same failure contract.

### Minor

1. **Residual divergence cases are under-described.** Risks only names DB-commit failure after successful panel update (spec lines 83-86), but the existing sync is multi-call: update/create may succeed and a later required operation such as traffic reset may fail (`app/cabinet/routes/admin_users.py:416-504`). An API timeout may also have an unknown remote outcome. The local rollback rule remains feasible, but the spec should explicitly classify these as accepted remote-state ambiguity/partial-panel-success risks so “failed” does not imply the panel is unchanged.
2. **“Commits once” needs a test boundary definition.** Acceptance criterion 3 and verification require one commit, while current nested services default to committing (`create_paid_subscription`, `extend_subscription`, and `reactivate_subscription` expose `commit=True` defaults at `app/database/crud/subscription.py:557-568`, `1043-1054`, and `1695-1721`). The transaction-safe-mode requirement is sound, but tests should count commits for the entire route/action execution, including nested services, rather than only the route or helper mock.

## Evidence

- Pinned spec blob SHA-256: `31370c4fea182c75bc8d0a730ff11a6e7299f4ba1d09ce7b98358e2c8c2f811c`.
- `_sync_subscription_to_panel` currently returns skipped/error dictionaries and commits internally: `app/cabinet/routes/admin_users.py:306-333`, `494-512`.
- Direct non-helper panel mutation paths cited above demonstrate that helper-call-site inventory is not equivalent to panel-relevant admin mutation inventory.
- `get_cabinet_db` yields a plain `AsyncSession` without an enclosing commit/rollback transaction manager (`app/cabinet/dependencies.py:29-35`), so explicit route/shared-boundary ownership is feasible and necessary.

## Verification

- `git rev-parse HEAD` returned the reviewed head exactly.
- `git cat-file -t fea9c32e12b9edb01939530b2cdda4e50f74b95e` returned `commit`.
- `git show <head>:docs/superpowers/specs/2026-08-02-admin-panel-sync-atomicity-design.md` succeeded and was reviewed in full.
- Read-only `rg`/line inspection covered every direct `_sync_subscription_to_panel` caller in `admin_users.py` and `admin_bulk_actions.py`, their bulk execution rollback boundaries, session dependency behavior, directly relevant committing CRUD services, and directly relevant alternate reset/delete panel paths.

## Residual Risks

- PostgreSQL and RemnaWave cannot be truly atomic under R1; remote side effects can survive local rollback or have an unknown outcome.
- Payment-provider cancellation side effects on cancel/reset/tariff-change paths cannot be rolled back with the DB and need explicit treatment when those routes are brought inside the clarified inventory.

## Next Action

Revise R1 to close the inventory boundary ambiguity and make acceptance/verification cover all panel-relevant admin route/actions, then resubmit the exact revised revision for independent review. Do not invoke `writing-plans` yet.
