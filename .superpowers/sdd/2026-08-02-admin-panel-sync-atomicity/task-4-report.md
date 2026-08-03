# Task 4 Report — Bulk Target Transaction Boundary

- **Task ID:** BEDOLAGA-PANEL-SYNC-ATOMICITY-T4
- **Base HEAD:** `4b571035adbb3e011b0b5e9087b1e1465e37ddd8`
- **Current HEAD:** `4f2fe5c9eca9be5efacfd5c78ba04123f79396a1`
- **Status:** `BLOCKED`
- **Timestamp:** `2026-08-03T01:06:42Z`

## Changed files

- `app/cabinet/routes/admin_bulk_actions.py`
- `app/services/user_service.py`
- `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- `tests/cabinet/test_bulk_change_tariff_preserves_period.py`
- `tests/services/test_platega_recurrent_cancel_hooks.py`

## Acceptance evidence

- Mandatory bulk handlers stage local work and the user/subscription target executors perform the sole successful commit.
- Typed skipped/failed panel outcomes roll back at the target boundary, emit bounded diagnostics, and return the safe panel-sync failure message.
- Subscription and user deletes fail closed on missing identity or unsuccessful panel work; grace checks remain in place.
- Bulk deletion passes `commit=False` to Platega and Lava cancellation helpers. The external provider cancellation remains an accepted irreversible residual; local work is rollback-capable.
- Parameterized contracts cover all mandatory bulk actions, two typed failure modes, success commit ownership, exact subscription dispatch, dry-run no-commit, and missing deletion identity.

## Verification

| Command | Result |
| --- | --- |
| `uv run pytest tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_platega_recurrent_cancel_hooks.py -q` | `64 passed` (32 pre-existing warnings) |
| `uv run ruff check app/cabinet/routes/admin_bulk_actions.py app/services/user_service.py tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_platega_recurrent_cancel_hooks.py` | passed |
| `git diff --check 4b571035adbb3e011b0b5e9087b1e1465e37ddd8..HEAD` | passed |

## Concerns

- Existing test-suite warnings remain, including async-mock warnings in Lava cancellation coverage.
- Remote payment cancellation cannot be compensated if later panel work or database commit fails.
- Initial fresh review findings were corrected: exact user-target diagnostic identity, subscription-target typed-failure coverage, and duplicate rollback from the transaction-safe full-delete helper.
- Fresh specification and quality re-reviews both remain blocked on one Important acceptance gap: the parameterized tests replace every actual handler with `AsyncMock`, so they prove executor control flow but do not demonstrate real handler local staging, panel invocation, no internal commit, and rollback for every mandatory mutation class. Exact-target success coverage likewise only runs the real dispatch shape for `SET_DEVICES`.

---

## Fix Round 1

- **Task ID:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-T4`
- **Base HEAD:** `4f2fe5c9eca9be5efacfd5c78ba04123f79396a1`
- **Current HEAD:** `6566daba838586dd4cde07a19eeb72fce7449394`
- **Status:** READY_FOR_FRESH_REVIEW
- **Timestamp:** `2026-08-03T01:21:29Z`

### Changed files

- `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- `.superpowers/sdd/2026-08-02-admin-panel-sync-atomicity/task-4-report.md`

### Shared-finding closure evidence

- Parameterized user-target success and typed `PanelSyncSkipped`/`PanelSyncFailed` failure cases invoke every real mandatory handler; no handler is replaced in `_ACTION_HANDLERS`.
- Each handler is checked for its own staged local work (`commit=False` CRUD calls or local field/delete staging), its exact panel action and target, and no handler-owned commit (the executor records exactly one commit only on success).
- Failure cases assert bounded safe non-success, one executor rollback, no commit, and restored staged subscription state. Grant failures retain the newly staged subscription identity for diagnostics; delete-user failures retain the executor's resolved target identity.
- Subscription-target success and failure cases cover every supported action and prove the selected subscription object, rather than the user's first subscription, reaches the real handler/panel operation.
- Delete-subscription checks the exact panel UUID; delete-user checks `force_panel_delete=True` and `commit=False` on its real handler's service boundary.

### Verification

| Command | Result |
| --- | --- |
| `uv run pytest tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_platega_recurrent_cancel_hooks.py -q` | `70 passed` (32 pre-existing warnings) |
| `uv run ruff check tests/cabinet/test_admin_bulk_panel_sync_contract.py` | `All checks passed!` |
| `git diff --check 4f2fe5c9eca9be5efacfd5c78ba04123f79396a1..HEAD` | passed |

### Concerns

- The pre-existing suite warnings (Pydantic deprecations and Lava async-mock warnings) remain unchanged.
- `uv.lock` remains user-owned dirty state and is excluded from this fix commit.
