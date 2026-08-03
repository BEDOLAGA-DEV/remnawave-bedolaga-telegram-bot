# PR Fix R4 Result Contract

- status: `DONE`
- task_id: `BEDOLAGA-PANEL-SYNC-ATOMICITY-PR-FIX-R4`
- agent_id: `K2SO`
- platform: `Web/full-stack`
- timestamp_utc: `2026-08-03T13:19:55Z`

## Implementation summary

Closed the three Important R3 review gaps without changing the approved R2
specification or plan. The subscription-target bulk executor now preserves a
typed exact sibling `subscription_id`, falling back to its requested target
only when the typed id is absent. The transitive CRUD inventory guard now
classifies both action-qualified and bare `admin_users.py` callers, with the
single inbound reconciliation false positive covered by a narrow documented
allowlist and the mutation proof running the production assertion path. Hidden
sibling trial panel disables now apply only to distinct multi-tariff panel
identities; single-tariff unified and bulk operations finish with the shared
user-level identity active after primary synchronization.

## Git and PR

- base_head: `232719bb124cd8781e73d0a511e8ba569296a704`
- prior_head: `31f4419f520323053d49bc26e0117ee9a8eea8aa`
- implementation_commit: `c4e49787ea325ed8d2110c7f413fd09509684020`
- implementation_head: `c4e49787ea325ed8d2110c7f413fd09509684020`
- current_head: the commit containing this contract; resolve with
  `git rev-parse HEAD` after the documentation commit.
- branch: `fix/admin-panel-sync-atomicity-r1`
- remote_branch: `origin/fix/admin-panel-sync-atomicity`
- pushed: `true`
- pr_url: `https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/pull/3134`
- pr_state_at_implementation_head: `OPEN`, non-draft, base `main`
- working_tree: clean after the contract commit.

## Files changed

- `app/cabinet/routes/admin_bulk_actions.py`
- `app/cabinet/routes/admin_users.py`
- `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- `tests/cabinet/test_admin_panel_sync_contract.py`
- `tests/cabinet/test_admin_panel_sync_inventory.py`
- `.superpowers/sdd/2026-08-02-admin-panel-sync-atomicity/pr-fix-r4-result-contract.md`

## Review finding mapping

| R3 Important finding | Code change | Regression evidence |
|---|---|---|
| Subscription-target bulk failures replace the exact sibling id with the requested primary id | `_execute_for_subscription` forwards `error.subscription_id` when present and uses `sub_id` only for `None` | Supported extend uses the subscription executor while grant retains its user executor; success/skipped/failed cases assert exact sibling UUID/id and bounded log, rollback with zero commits on failure, and one executor commit on success. Existing selected-target failures prove the absent-id fallback. |
| The production transitive CRUD inventory guard silently filters all bare `admin_users.py` callers | `_assert_production_hidden_crud_inventory` classifies the full unified caller set against exact keys/routes plus only `ADMIN_USERS_HIDDEN_CRUD_READ_ONLY_CALLERS={'sync_user_from_panel'}`; bulk callers remain route-classified | The mutation proof appends a standalone `new_admin_paid_mutation -> create_paid_subscription` caller to the real production AST and requires the same production assertion helper to reject it until classified or explicitly excluded. |
| Single-tariff sibling cleanup disables the shared user UUID after primary sync | `_sync_deactivated_sibling_trials_to_panel` performs exact sibling disables only when multi-tariff mode gives them distinct identities | Public unified paid-plus-sibling extend and public bulk trial-to-paid grant regressions model the shared remote state, require only `primary-active`, assert no disable call, one commit, and final remote `active`; prior multi-tariff success/skipped/failed rollback tests remain green. |

## TDD evidence

```text
typed sibling id RED:
  2 failed, 4 passed
  observed subscription-target response id 101 instead of sibling id 202

production inventory mutation RED:
  1 failed, 1 passed
  expected AssertionError was not raised for new_admin_paid_mutation

single-tariff shared identity RED:
  2 failed
  observed events ['primary-active', 'shared-disabled']

targeted GREEN after minimal fixes:
  17 passed, 192 deselected, 28 warnings
```

## Fresh verification

```text
uv run --frozen pytest -q <12 focused atomicity suites>
  342 passed, 50 warnings in 11.01s (exit 0)

uv run --frozen ruff format --check <5 changed Python files>
  5 files already formatted (exit 0)
uv run --frozen ruff check <5 changed Python files>
  All checks passed! (exit 0)
uv run --frozen python -m compileall -q <5 changed Python files>
  exit 0

git diff --check 31f4419f...<implementation worktree>
git diff --check 232719bb...<implementation worktree>
  exit 0 (both)

R4 exact-path scope guard
  exit 0; only the five listed Python files changed before this contract
R4 added-line credential-pattern guard
  exit 0; no matches

git hash-object <approved specification>
  34604d4d797594333bb1411d3ef0a15aeb7e6d8a
git hash-object <approved plan>
  f45d2e6284b77069867503137b7ee5879194199e
```

Implementation push verification:

```text
local implementation head                 c4e49787ea325ed8d2110c7f413fd09509684020
origin/fix/admin-panel-sync-atomicity      c4e49787ea325ed8d2110c7f413fd09509684020
PR #3134 headRefOid                       c4e49787ea325ed8d2110c7f413fd09509684020
PR #3134 state                            OPEN, non-draft, base main
```

## Risks, concerns, and next gate

- blockers: none in the bounded R4 implementation.
- concerns: the focused suite retains 50 known dependency/runtime warnings,
  including payment-test unawaited-`AsyncMock` warnings. GitHub checks were
  queued on the implementation head when the PR was verified.
- accepted R2 architectural risks remain: remote success followed by local
  commit failure, partial multi-call remote success, timeout ambiguity, and
  irreversible payment-provider effects.
- review_gate: dispatch fresh exact-head specification-compliance and
  code-quality reviews, then a fresh Chewbacca actual-PR review before delivery.
- plane_update_recommendation: record the final contract-containing head,
  verification above, and pending fresh review gate for PR #3134.
- plane_written: `false`
