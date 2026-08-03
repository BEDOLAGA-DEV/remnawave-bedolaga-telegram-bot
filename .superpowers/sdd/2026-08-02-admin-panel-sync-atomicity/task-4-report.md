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

---

## Fix Round 2

- **Task ID:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-T4-FIX-R2`
- **Base HEAD:** `928bdbb05ca68d47c242b70877e822001db30226`
- **Current implementation HEAD:** `a4042d1ee65e08fc87cc1e5d6166e68d16de024f`
- **Status:** `READY_FOR_FRESH_REVIEW`
- **Timestamp:** `2026-08-03T01:30:11Z`

### Changed files

- `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- `.superpowers/sdd/2026-08-02-admin-panel-sync-atomicity/task-4-report.md`

### Clause-by-clause review closure

1. **Preserved success response:** both user and subscription target-executor matrices now parameterize all supported mandatory actions and assert their exact established success message.
2. **Multi-tariff delete direct path:** real-handler tests cover selected subscription UUID success plus typed `PanelSyncFailed`, false, and exception outcomes. Failures prove a non-success response, one rollback, no commit, and no local delete execution.
3. **Late add-traffic direct enable:** multi-tariff tests assert `_sync_subscription_to_panel(db, user, selected, action='add_traffic')`, exact `enable_remnawave_user(selected.remnawave_uuid, db=db)`, and success/false/exception contracts. Late failures restore staged subscription state, roll back once, and do not commit.
4. **Executor commit ownership/order:** commit instrumentation inspects the active production call site and fails unless `_execute_for_user` or `_execute_for_subscription` is the direct owner after the final real handler/panel success event. This distinguishes an internal handler commit from the target-boundary commit.
5. **Complete subscription sync contracts:** success and typed-failure matrices assert the full `_sync_subscription_to_panel(db, user, selected, action=...)` invocation for every sync-backed subscription action, including `reset_traffic=False` for tariff change; delete-subscription is asserted against its direct disable path.

### Verification

| Command | Result |
| --- | --- |
| Mutation check: temporarily replace the extend success response, then run `uv run pytest -p no:cacheprovider tests/cabinet/test_admin_bulk_panel_sync_contract.py -q -k 'user_handler_stages_panel_work_and_commits_once or subscription_handler_success_uses_selected_subscription_and_commits_once'` | expected RED: `2 failed, 14 passed`; production text restored before final verification |
| `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_platega_recurrent_cancel_hooks.py -q` | `77 passed` (32 pre-existing warnings) |
| `uv run ruff check tests/cabinet/test_admin_bulk_panel_sync_contract.py` | `All checks passed!` |
| `git diff --check 928bdbb05ca68d47c242b70877e822001db30226 -- tests/cabinet/test_admin_bulk_panel_sync_contract.py` | passed |

### Concerns

- The existing warning set remains: Pydantic deprecations plus four known Lava async-mock resource warnings.
- `uv.lock` was already dirty, remains unstaged, and is intentionally excluded.

---

## Fix Round 3

- **Task ID:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-T4-FIX-R3`
- **Base HEAD:** `c4a3f5d6f0a22bf52dcaeeca976a2a0a8fd45231`
- **Current implementation HEAD:** `9704be538445ca0d46e1075bf1cbeb831d92cc18`
- **Status:** `READY_FOR_FRESH_REVIEW`
- **Timestamp:** `2026-08-03T01:36:24Z`

### Changed files

- `app/cabinet/routes/admin_bulk_actions.py`
- `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- `.superpowers/sdd/2026-08-02-admin-panel-sync-atomicity/task-4-report.md`

### I1 closure evidence

- `_do_delete_subscription` now catches an exception raised by `disable_remnawave_user`, emits only a bounded local diagnostic (`user_id`, `subscription_id`), and raises `PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)` chained from the original error.
- The subscription executor consequently reaches its typed panel-failure boundary, which performs exactly one rollback, no commit, no local delete, returns `panel_sync_failure_message()` (the local mutation was not saved), and emits the standard bounded diagnostic containing exact `user_id`, selected `subscription_id`, `delete_subscription` action, and `panel_api_failed` reason code.
- The real multi-tariff direct-disable contract covers typed, false, and raised outcomes; the raised `RuntimeError` was RED before the normalization (`Action failed: internal error`) and GREEN after it.

### Verification

| Command | Result |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/cabinet/test_admin_bulk_panel_sync_contract.py -q -k 'multi_tariff_subscription_delete_failure_rolls_back_before_local_delete'` before production change | expected RED: `1 failed, 2 passed` — raised disable returned `Action failed: internal error` |
| `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_platega_recurrent_cancel_hooks.py -q` | `77 passed` (32 pre-existing warnings) |
| `uv run ruff check app/cabinet/routes/admin_bulk_actions.py tests/cabinet/test_admin_bulk_panel_sync_contract.py` | `All checks passed!` |
| `git diff --check c4a3f5d6f0a22bf52dcaeeca976a2a0a8fd45231 -- app/cabinet/routes/admin_bulk_actions.py tests/cabinet/test_admin_bulk_panel_sync_contract.py` | passed |

### Concerns

- The existing warning set remains: Pydantic deprecations plus four known Lava async-mock resource warnings.
- `uv.lock` was already dirty, remains unstaged, and is intentionally excluded.
