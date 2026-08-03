# Admin Mutation and RemnaWave Sync Atomicity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every panel-relevant admin subscription mutation fail closed: commit the local mutation exactly once only after required RemnaWave work succeeds, otherwise roll it back and return a safe non-success response.

**Architecture:** Introduce one typed synchronization contract and one executable inventory that names every mandatory route/action and its transaction owner. Refactor the shared panel helper to be transaction-neutral, then move commit/rollback ownership to the single-user and bulk action boundaries while preserving exact subscription targeting. Parameterized contract tests consume the same inventory, so a new panel-relevant admin mutation cannot remain unclassified.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy async, pytest/anyio, `uv`, Ruff, mypy.

## Global Constraints

- Approved specification: `docs/superpowers/specs/2026-08-02-admin-panel-sync-atomicity-design.md`, revision R2, blob `34604d4d797594333bb1411d3ef0a15aeb7e6d8a`.
- Every inventoried route/action is `mandatory-sync`; the R2 best-effort exception list is empty.
- A mandatory action follows: local mutation without commit, exact-subscription panel sync, exactly one caller-owned commit on success, rollback and non-success on skipped/failed sync.
- Shared helpers and services must not commit before the caller-owned final commit; unrelated callers must retain their existing behavior through explicit `commit: bool = True` compatibility parameters where needed.
- Failure responses state that the local mutation was not saved because panel synchronization did not complete and must not expose exception text, credentials, secret-bearing URLs, or RemnaWave payloads.
- Failure diagnostics contain `user_id`, exact `subscription_id`, admin `action`, and a bounded `reason_code`; raw secrets are forbidden.
- Multi-tariff mode always uses `subscription.remnawave_uuid`; it must never substitute `user.remnawave_uuid` when the subscription identity is absent.
- PostgreSQL, RemnaWave, and payment providers are not distributed-atomic. Remote success followed by DB commit failure, partial remote success, timeout ambiguity, and irreversible payment cancellation remain accepted residual risks.
- Keep transaction read/write sets narrow and retain existing RemnaWave timeouts.

## File Structure

- Create `app/services/admin_panel_sync.py`: typed outcomes/exceptions, bounded reason codes, safe failure text, inventory entry type, and the immutable mandatory-action inventory.
- Modify `app/cabinet/routes/admin_users.py`: transaction-neutral `_sync_subscription_to_panel` plus caller-owned single-user transaction boundaries.
- Modify `app/cabinet/routes/admin_bulk_actions.py`: caller-owned bulk target transaction boundaries and mandatory delete/disable behavior.
- Modify `app/services/subscription_service.py`: transaction-safe reset path and typed panel failure propagation.
- Modify payment/CRUD helpers only where discovery proves an internal commit lies on a mandatory path; add explicit `commit=False` without changing default behavior.
- Create `tests/cabinet/test_admin_panel_sync_contract.py`: typed helper, diagnostics, exact targeting, and single-user route contracts.
- Create `tests/cabinet/test_admin_panel_sync_inventory.py`: executable inventory completeness and source-discovery guard.
- Create `tests/cabinet/test_admin_bulk_panel_sync_contract.py`: parameterized bulk success/skipped/failed transaction contracts.
- Modify existing focused tests where signatures acquire a `commit` keyword.

---

### Task 1: Executable Mandatory-Action Inventory and Typed Contract

**Files:**
- Create: `app/services/admin_panel_sync.py`
- Create: `tests/cabinet/test_admin_panel_sync_inventory.py`
- Create: `tests/cabinet/test_admin_panel_sync_contract.py`

**Interfaces:**
- Consumes: no new application interface.
- Produces: `PanelSyncReason(StrEnum)`, `PanelSyncSkipped`, `PanelSyncFailed`, `AdminPanelMutation`, `MANDATORY_ADMIN_PANEL_MUTATIONS`, `BEST_EFFORT_ADMIN_PANEL_MUTATIONS`, and `panel_sync_failure_message()`.

- [ ] **Step 1: Write the failing inventory and typed-contract tests**

```python
from app.services.admin_panel_sync import (
    BEST_EFFORT_ADMIN_PANEL_MUTATIONS,
    MANDATORY_ADMIN_PANEL_MUTATIONS,
    AdminPanelMutation,
    PanelSyncFailed,
    PanelSyncReason,
    PanelSyncSkipped,
    panel_sync_failure_message,
)


REQUIRED_MUTATION_CLASSES = {
    "create", "extend", "set_end_date", "activate", "cancel", "reset",
    "change_tariff", "set_traffic", "set_devices", "delete_subscription",
    "delete_user", "disable_user",
}


def test_r2_inventory_is_complete_and_has_no_best_effort_entries():
    assert BEST_EFFORT_ADMIN_PANEL_MUTATIONS == ()
    assert REQUIRED_MUTATION_CLASSES <= {entry.mutation_class for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}
    assert all(entry.classification == "mandatory-sync" for entry in MANDATORY_ADMIN_PANEL_MUTATIONS)
    assert len({entry.key for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}) == len(MANDATORY_ADMIN_PANEL_MUTATIONS)
    assert all(entry.route and entry.action and entry.integration_path and entry.transaction_owner for entry in MANDATORY_ADMIN_PANEL_MUTATIONS)


def test_typed_failures_are_bounded_and_safe():
    skipped = PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED)
    failed = PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED)
    assert skipped.reason_code is PanelSyncReason.NOT_CONFIGURED
    assert failed.reason_code is PanelSyncReason.PANEL_API_FAILED
    message = panel_sync_failure_message()
    assert "not saved" in message.lower()
    assert "token" not in message.lower()
```

- [ ] **Step 2: Run the new tests and verify they fail because the module is absent**

Run: `uv run pytest tests/cabinet/test_admin_panel_sync_inventory.py tests/cabinet/test_admin_panel_sync_contract.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: app.services.admin_panel_sync`.

- [ ] **Step 3: Implement the contract and enumerate every discovered action**

```python
from dataclasses import dataclass
from enum import StrEnum


class PanelSyncReason(StrEnum):
    NOT_CONFIGURED = "not_configured"
    MISSING_SUBSCRIPTION_UUID = "missing_subscription_uuid"
    PANEL_API_FAILED = "panel_api_failed"
    PANEL_RESPONSE_INVALID = "panel_response_invalid"
    PANEL_TIMEOUT_UNKNOWN = "panel_timeout_unknown"


class PanelSyncError(RuntimeError):
    def __init__(self, reason_code: PanelSyncReason) -> None:
        super().__init__(reason_code.value)
        self.reason_code = reason_code


class PanelSyncSkipped(PanelSyncError):
    pass


class PanelSyncFailed(PanelSyncError):
    pass


@dataclass(frozen=True, slots=True)
class AdminPanelMutation:
    route: str
    action: str
    mutation_class: str
    integration_path: str
    transaction_owner: str
    classification: str = "mandatory-sync"

    @property
    def key(self) -> str:
        return f"{self.route}:{self.action}"


def panel_sync_failure_message() -> str:
    return "The local mutation was not saved because panel synchronization did not complete."
```

Populate `MANDATORY_ADMIN_PANEL_MUTATIONS` with every semantically discovered mutation in `admin_users.py`, `admin_bulk_actions.py`, and called services. At minimum include single-route actions `create`, `extend`, `set_end_date`, `change_tariff`, `set_traffic`, `cancel`, `activate`, `reset`, subscription/user deletion, user disable, device-limit/reset operations, and bulk actions `grant_subscription`, `extend`, `activate`, `cancel`, `change_tariff`, `add_traffic`, `set_devices`, `delete_subscription`, and `delete_user`. Record exact handler/action names, integration paths, and final commit owners; document any examined false positive (for example read-only `sync_user_from_panel`) in the test beside the discovery guard.

- [ ] **Step 4: Add an executable source-discovery guard**

```python
import ast
from pathlib import Path


def test_panel_relevant_admin_handlers_are_explicitly_classified():
    roots = (
        Path("app/cabinet/routes/admin_users.py"),
        Path("app/cabinet/routes/admin_bulk_actions.py"),
    )
    discovered = set()
    needles = {"_sync_subscription_to_panel", "disable_remnawave_user", "reset_subscription_with_panel"}
    for path in roots:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                called = {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))}
                if called & needles:
                    discovered.add(node.name)
    classified = {entry.route for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}
    assert discovered <= classified
```

Extend the explicit discovery needles to the direct RemnaWave service calls found during implementation; do not use `_sync_subscription_to_panel` as the sole discovery mechanism.

- [ ] **Step 5: Run and commit the contract**

Run: `uv run pytest tests/cabinet/test_admin_panel_sync_inventory.py tests/cabinet/test_admin_panel_sync_contract.py -q`

Expected: PASS.

```bash
git add app/services/admin_panel_sync.py tests/cabinet/test_admin_panel_sync_inventory.py tests/cabinet/test_admin_panel_sync_contract.py
git commit -m "test: define mandatory admin panel sync contract"
```

### Task 2: Transaction-Neutral Shared Sync and Exact-Subscription Failures

**Files:**
- Modify: `app/cabinet/routes/admin_users.py:306`
- Modify: `tests/cabinet/test_admin_panel_sync_contract.py`
- Modify: `tests/cabinet/test_remnawave_sync_timeout.py`

**Interfaces:**
- Consumes: `PanelSyncSkipped`, `PanelSyncFailed`, and `PanelSyncReason` from Task 1.
- Produces: `_sync_subscription_to_panel(...) -> dict[str, object]` that never commits, raises typed failures, treats required traffic reset as part of success, and logs bounded context.

- [ ] **Step 1: Write failing helper contract tests**

```python
@pytest.mark.anyio
async def test_sync_not_configured_raises_skipped_without_commit(monkeypatch, user, subscription, db):
    monkeypatch.setattr(RemnaWaveService, "is_configured", False)
    with pytest.raises(PanelSyncSkipped) as raised:
        await admin_users._sync_subscription_to_panel(db, user, subscription)
    assert raised.value.reason_code is PanelSyncReason.NOT_CONFIGURED
    db.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_required_traffic_reset_failure_raises_failed(monkeypatch, user, subscription, db, api):
    api.reset_user_traffic.side_effect = TimeoutError("secret=https://panel/?token=x")
    with pytest.raises(PanelSyncFailed) as raised:
        await admin_users._sync_subscription_to_panel(db, user, subscription, reset_traffic=True)
    assert raised.value.reason_code is PanelSyncReason.PANEL_TIMEOUT_UNKNOWN
    db.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_multi_tariff_missing_subscription_uuid_never_uses_user_uuid(monkeypatch, user, subscription, db):
    monkeypatch.setattr(settings, "is_multi_tariff_enabled", lambda: True)
    subscription.remnawave_uuid = None
    user.remnawave_uuid = "wrong-user-level-uuid"
    with pytest.raises(PanelSyncSkipped) as raised:
        await admin_users._sync_subscription_to_panel(db, user, subscription, reset_traffic=True)
    assert raised.value.reason_code is PanelSyncReason.MISSING_SUBSCRIPTION_UUID
```

- [ ] **Step 2: Run the focused tests and verify premature commit/silent-return failures**

Run: `uv run pytest tests/cabinet/test_admin_panel_sync_contract.py tests/cabinet/test_remnawave_sync_timeout.py -q`

Expected: FAIL because the helper currently commits, returns dictionaries for errors/skips, and swallows traffic-reset failures.

- [ ] **Step 3: Refactor `_sync_subscription_to_panel` to fail typed and never commit**

Remove `await db.commit()`. Raise `PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED)` for missing configuration. Convert bounded API/validation failures to `PanelSyncFailed` without embedding raw exception text. For a required reset, use only the mode-correct exact UUID and raise when it is absent or when `reset_user_traffic` fails. Keep successful return data for callers that need it, but make typed exceptions the only skipped/failed contract.

- [ ] **Step 4: Add structured diagnostic assertions**

Capture logs and assert the failure event contains exactly `user_id`, `subscription_id`, `action`, and `reason_code`; assert serialized logs contain neither a supplied token value nor a supplied secret-bearing URL. Pass the admin action into the helper as a required keyword-only `action: str` so every diagnostic is attributable.

- [ ] **Step 5: Run and commit the shared boundary**

Run: `uv run pytest tests/cabinet/test_admin_panel_sync_contract.py tests/cabinet/test_remnawave_sync_timeout.py -q`

Expected: PASS, with commit count zero inside the helper.

```bash
git add app/cabinet/routes/admin_users.py tests/cabinet/test_admin_panel_sync_contract.py tests/cabinet/test_remnawave_sync_timeout.py
git commit -m "refactor: make admin panel sync transaction neutral"
```

### Task 3: Single-User Admin Mutation Transaction Boundary

**Files:**
- Modify: `app/cabinet/routes/admin_users.py:1182`
- Modify: `app/cabinet/routes/admin_users.py:2692`
- Modify: `app/cabinet/routes/admin_users.py:2742`
- Modify: `app/cabinet/routes/admin_users.py:2803`
- Modify: `app/cabinet/routes/admin_users.py:2871`
- Modify: `app/cabinet/routes/admin_users.py:2985`
- Modify: `app/services/subscription_service.py:1318`
- Modify: payment/CRUD helpers discovered on these mandatory paths, preserving `commit=True` defaults
- Modify: `tests/cabinet/test_admin_panel_sync_contract.py`
- Modify: `tests/services/test_reset_subscription.py`
- Modify: signature-focused payment tests when `commit=False` is added

**Interfaces:**
- Consumes: Task 2 `_sync_subscription_to_panel(..., *, action: str)` and typed failures.
- Produces: caller-owned single-route transactions and `reset_subscription_with_panel(..., *, commit: bool = True)` compatibility behavior.

- [ ] **Step 1: Add parameterized failure contracts for every inventoried single route/action**

```python
@pytest.mark.anyio
@pytest.mark.parametrize("action", [
    "create", "extend", "set_end_date", "change_tariff", "set_traffic",
    "cancel", "activate",
])
@pytest.mark.parametrize("failure", [
    PanelSyncSkipped(PanelSyncReason.NOT_CONFIGURED),
    PanelSyncFailed(PanelSyncReason.PANEL_API_FAILED),
])
async def test_single_mutation_sync_failure_rolls_back_without_false_success(
    action, failure, route_request, monkeypatch, db
):
    monkeypatch.setattr(admin_users, "_sync_subscription_to_panel", AsyncMock(side_effect=failure))
    result = await admin_users.update_user_subscription(request=route_request(action), db=db, **route_dependencies())
    assert result.success is False
    assert "not saved" in result.message.lower()
    assert action_success_phrase(action) not in result.message.lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
```

Add equivalent tests for `reset_user_subscription`, `reset_user_trial`, `delete_user`, `full_delete_user`, `disable_user`, and the device mutation routes identified by Task 1. Snapshot local fields/related records before the call and assert rollback restores or removes every staged change.

- [ ] **Step 2: Add success contracts for exactly one commit and exact target identity**

For every single-route mutation class, assert call order `local mutation -> panel operation -> db.commit`, exactly one commit across nested services, preserved prior success response, and in multi-tariff mode the exact `subscription.id`/`subscription.remnawave_uuid` is used.

- [ ] **Step 3: Run focused tests and verify they expose premature commits and false success**

Run: `uv run pytest tests/cabinet/test_admin_panel_sync_contract.py tests/services/test_reset_subscription.py -q`

Expected: FAIL for routes/services that commit before sync, swallow panel failure, or return success after rollback-worthy failure.

- [ ] **Step 4: Implement the route-owned boundary**

Move each mandatory path to this shape:

```python
try:
    await apply_local_mutation_without_commit(...)
    await _sync_subscription_to_panel(db, user, subscription, action=request.action)
    await db.commit()
except (PanelSyncSkipped, PanelSyncFailed) as exc:
    await db.rollback()
    log_panel_sync_failure(user.id, subscription.id, request.action, exc.reason_code)
    return UpdateSubscriptionResponse(success=False, message=panel_sync_failure_message(), ...)
```

Add `commit: bool = True` to nested mutation/payment helpers that currently commit: use `flush()` when identifiers are needed and let the route pass `commit=False`; leave all unrelated callers on the default. For destructive routes, require panel disable/sync success before staging the local delete. Preserve existing grace-access guards and document irreversible payment cancellation as residual risk rather than reporting end-to-end success.

- [ ] **Step 5: Run single-route and regression suites**

Run: `uv run pytest tests/cabinet/test_admin_panel_sync_contract.py tests/services/test_reset_subscription.py tests/services/test_platega_recurrent_cancel_hooks.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the single-route boundary**

```bash
git add app/cabinet/routes/admin_users.py app/services/subscription_service.py app/services/payment tests/cabinet/test_admin_panel_sync_contract.py tests/services/test_reset_subscription.py tests/services/test_platega_recurrent_cancel_hooks.py
git commit -m "fix: fail closed for admin subscription mutations"
```

Stage only payment/CRUD files actually changed; do not include unrelated files matched by the directory path.

### Task 4: Bulk Target Transaction Boundary

**Files:**
- Modify: `app/cabinet/routes/admin_bulk_actions.py:125`
- Modify: `app/cabinet/routes/admin_bulk_actions.py:840`
- Modify: `app/cabinet/routes/admin_bulk_actions.py:892`
- Create: `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- Modify: `tests/cabinet/test_bulk_change_tariff_preserves_period.py`
- Modify: `tests/services/test_platega_recurrent_cancel_hooks.py`

**Interfaces:**
- Consumes: Tasks 1-2 contract/inventory and Task 3 transaction-safe nested helpers.
- Produces: one isolated transaction result per bulk user/subscription target; a failed target rolls back and reports non-success without committing its local changes.

- [ ] **Step 1: Write parameterized bulk target contracts**

```python
MANDATORY_BULK_ACTIONS = (
    BulkActionType.EXTEND_SUBSCRIPTION,
    BulkActionType.CANCEL_SUBSCRIPTION,
    BulkActionType.ACTIVATE_SUBSCRIPTION,
    BulkActionType.CHANGE_TARIFF,
    BulkActionType.ADD_TRAFFIC,
    BulkActionType.SET_DEVICES,
    BulkActionType.DELETE_SUBSCRIPTION,
    BulkActionType.GRANT_SUBSCRIPTION,
    BulkActionType.DELETE_USER,
)


@pytest.mark.anyio
@pytest.mark.parametrize("action", MANDATORY_BULK_ACTIONS)
@pytest.mark.parametrize("failure", ["skipped", "failed"])
async def test_bulk_target_panel_failure_rolls_back_and_reports_non_success(action, failure, harness):
    harness.panel_failure(failure)
    result = await harness.execute(action)
    assert result.success is False
    assert "not saved" in result.message.lower()
    harness.db.rollback.assert_awaited_once()
    harness.db.commit.assert_not_awaited()
    harness.assert_local_state_unchanged()
```

Add success cases for each mutation class asserting exactly one commit and retained success text. Run both `_execute_for_user` and `_execute_for_subscription` where the action supports both, proving the selected subscription is not replaced by the user's first subscription.

- [ ] **Step 2: Run the new bulk tests and verify current false-success behavior**

Run: `uv run pytest tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py -q`

Expected: FAIL because handlers currently commit before sync and several paths swallow panel failures.

- [ ] **Step 3: Move commit ownership to each bulk target execution**

Make `_do_*` handlers stage changes and perform required panel work without committing. Let `_execute_for_user` and `_execute_for_subscription` commit once only after a successful mandatory handler result. Catch typed sync failures at that target boundary, rollback, emit bounded diagnostics, and return `BulkUserResult(success=False, message=panel_sync_failure_message())`. Dry-run paths remain non-mutating and perform neither panel calls nor commit.

- [ ] **Step 4: Make bulk delete fail closed**

For subscription/user deletion, treat missing required UUID/configuration and RemnaWave disable/API failure as typed failure. Perform required panel work before staging deletes; never log-and-continue. Preserve grace-access checks. Invoke payment cancellation helpers with `commit=False` where supported and record provider irreversibility in the implementation result.

- [ ] **Step 5: Run bulk, multi-tariff, and payment regression tests**

Run: `uv run pytest tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_platega_recurrent_cancel_hooks.py -q`

Expected: PASS, including exact subscription targeting for streamed and non-streamed bulk execution.

- [ ] **Step 6: Commit the bulk boundary**

```bash
git add app/cabinet/routes/admin_bulk_actions.py tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_platega_recurrent_cancel_hooks.py
git commit -m "fix: make bulk panel sync failures roll back"
```

### Task 5: Whole-Inventory Verification and Implementation Evidence

**Files:**
- Modify: `tests/cabinet/test_admin_panel_sync_inventory.py`
- Modify: `tests/cabinet/test_admin_panel_sync_contract.py`
- Modify: `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- Create: `docs/superpowers/ledgers/bedolaga-panel-sync-atomicity-implementation.md`

**Interfaces:**
- Consumes: final inventory and all transaction boundaries from Tasks 1-4.
- Produces: executable coverage for every inventory key and a review-ready implementation ledger pinned to the delivered HEAD.

- [ ] **Step 1: Add the inventory-to-contract coverage assertion**

```python
def test_every_inventory_key_has_success_skipped_and_failed_contract_coverage():
    required = {entry.key for entry in MANDATORY_ADMIN_PANEL_MUTATIONS}
    assert required == SUCCESS_CASE_KEYS
    assert required == SKIPPED_CASE_KEYS
    assert required == FAILED_CASE_KEYS
```

Build the three case-key sets from the parameter tables actually executed by the single and bulk contract suites, not from a duplicate hand-written list.

- [ ] **Step 2: Run all focused admin synchronization tests**

Run: `uv run pytest tests/cabinet/test_admin_panel_sync_inventory.py tests/cabinet/test_admin_panel_sync_contract.py tests/cabinet/test_admin_bulk_panel_sync_contract.py tests/cabinet/test_remnawave_sync_timeout.py tests/cabinet/test_bulk_change_tariff_preserves_period.py tests/services/test_reset_subscription.py tests/services/test_platega_recurrent_cancel_hooks.py -q`

Expected: PASS.

- [ ] **Step 3: Run repository-required checks**

Run: `uv run ruff format --check app tests`

Expected: exit 0.

Run: `uv run ruff check app tests`

Expected: exit 0.

Run: `uv run mypy app`

Expected: exit 0, or record a repository-baseline failure with exact unchanged evidence and still run focused type checking on changed modules.

- [ ] **Step 4: Write the implementation ledger**

Record `task_id`, approved spec revision/blob, branch, HEAD, inventory table (route/action, mutation class, integration path, transaction owner, classification), empty best-effort list, files changed, focused/full verification, skipped checks with reasons, structured-log redaction evidence, exact-subscription evidence, accepted residual risks, and `plane_written: false` with the exact recommended Plane update. Do not include tokens, raw URLs with credentials, payloads, or private logs.

- [ ] **Step 5: Commit the final evidence**

```bash
git add tests/cabinet/test_admin_panel_sync_inventory.py tests/cabinet/test_admin_panel_sync_contract.py tests/cabinet/test_admin_bulk_panel_sync_contract.py docs/superpowers/ledgers/bedolaga-panel-sync-atomicity-implementation.md
git commit -m "docs: record admin panel sync verification"
```

- [ ] **Step 6: Verify the final tree before review**

Run: `git status --short && git log -5 --oneline`

Expected: clean working tree and the task commits visible. Record the exact HEAD for specification-compliance, code-quality, and final Chewbacca PR review.
