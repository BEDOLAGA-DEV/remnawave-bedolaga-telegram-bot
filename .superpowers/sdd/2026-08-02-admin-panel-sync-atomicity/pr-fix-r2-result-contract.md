# PR Fix R2 Result Contract

- status: `DONE`
- task_id: `BEDOLAGA-PANEL-SYNC-ATOMICITY-PR-FIX-R2`
- agent_id: `K2SO`
- platform: `Web/full-stack`
- timestamp_utc: `2026-08-03T12:10:25Z`

## Implementation summary

Resolved every Important finding reported by the fresh R1 specification-compliance
and code-quality reviews. Single-user traffic addition now stages the local
mutation, completes shared sync and required direct enable, and commits once at
the route boundary. Mandatory full-delete and reset routes no longer honor their
legacy panel opt-outs. Strict reset treats the panel service's normal `False`
result as a typed failure before local reset. Bulk cancel, activate, and
set-devices no longer refresh away their staged mutations; real AsyncSession
regressions cover all three handlers.

## Git and PR

- base_head: `232719bb124cd8781e73d0a511e8ba569296a704`
- prior_head: `e90cb3268b725475cdbe91d8d3d01591eaf41dd2`
- implementation_commit: `27ec028da751477c78d3c845bfeb51d569f7348a`
- branch: `fix/admin-panel-sync-atomicity-r1`
- remote_branch: `fix/admin-panel-sync-atomicity`
- commit/current_head: the commit containing this contract; resolve with
  `git rev-parse HEAD` after the documentation commit.
- pushed: `true`
- pr_url: `https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/pull/3134`
- working_tree: clean after the contract commit.

## Files changed

- `app/cabinet/routes/admin_bulk_actions.py`
- `app/cabinet/routes/admin_users.py`
- `app/services/subscription_service.py`
- `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- `tests/cabinet/test_admin_panel_sync_contract.py`
- `tests/services/test_reset_subscription.py`
- `.superpowers/sdd/2026-08-02-admin-panel-sync-atomicity/pr-fix-r2-result-contract.md`

## Review finding mapping

| Review finding | Fix | Regression evidence |
|---|---|---|
| Spec Important 1: single `add_traffic` commits before required enable | Shared sync and exact-target direct enable now run before the sole route commit; missing UUID, `False`, and exception raise typed outcomes and roll back | `test_single_add_traffic_late_enable_failure_rolls_back_without_commit`; existing unified success matrix |
| Spec Important 2: full-delete panel opt-out | Route always passes `force_panel_delete=True`; success requires both bot and panel deletion | `test_full_delete_explicit_false_cannot_bypass_mandatory_panel_delete` |
| Spec Important 3: reset panel opt-out | Route always runs exact-target mandatory disable before local deletion, including explicit `deactivate_in_panel=False` | `test_standalone_reset_explicit_false_cannot_bypass_mandatory_panel_disable` |
| Quality Important 1: strict reset accepts `False` | `reset_subscription_with_panel(commit=False)` raises `PanelSyncFailed(PANEL_API_FAILED)` before `reset_subscription` on `False` | `test_reset_with_panel_commit_false_rejects_false_before_local_reset`; `test_unified_reset_panel_failure_rolls_back_before_local_reset` for `False` and exception |
| Quality Important 2: bulk refresh discards staged state | Removed the three unsafe full-object refreshes before sync | `test_bulk_staged_mutation_survives_real_async_session_until_panel_sync`, parameterized for cancel, activate, and set-devices using a real SQLAlchemy AsyncSession |
| Quality Important 3: two mandatory actions caller-bypassable | Same mandatory full-delete/reset enforcement as Spec Important 2 and 3 | Both explicit-`False` public-route regressions above |

## Verification

```text
uv run pytest -q <11 focused atomicity suites>
  303 passed, 50 warnings in 10.38s (exit 0)

uv run pytest -q tests/cabinet/test_admin_panel_sync_contract.py \
  tests/cabinet/test_admin_bulk_panel_sync_contract.py \
  tests/services/test_reset_subscription.py
  202 passed, 46 warnings in 5.98s (exit 0)

uv run ruff format --check <6 changed Python files>
  6 files already formatted (exit 0)
uv run ruff check <6 changed Python files>
  All checks passed (exit 0)
uv run python -m compileall -q <6 changed Python files>
  exit 0
git diff --check
  exit 0
```

Remote/PR verification after the implementation push:

```text
origin/fix/admin-panel-sync-atomicity = 27ec028da751477c78d3c845bfeb51d569f7348a
PR #3134 headRefOid                    = 27ec028da751477c78d3c845bfeb51d569f7348a
PR #3134 state                         = OPEN, non-draft, base main
```

## Residual risks / blockers

- blockers: none.
- concerns: the focused suite retains 50 pre-existing dependency/runtime
  warnings, including payment-test `AsyncMock` warnings. No focused failure
  occurred.
- accepted R2 risks remain: remote success followed by local commit failure,
  partial multi-call remote success, timeout ambiguity, and irreversible
  payment-provider effects.

- plane_update_recommendation: record this corrected exact head and request
  fresh isolated specification-compliance and code-quality reviews, followed
  by fresh Chewbacca review of PR #3134.
- plane_written: `false`
