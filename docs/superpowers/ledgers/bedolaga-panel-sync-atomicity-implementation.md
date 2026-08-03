# Admin Panel Sync Atomicity — Current Implementation Evidence

- **Task ID:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-T5-FIX-R4`
- **Approved specification:** R2, `docs/superpowers/specs/2026-08-02-admin-panel-sync-atomicity-design.md`
- **Specification blob:** `34604d4d797594333bb1411d3ef0a15aeb7e6d8a`
- **Branch:** `fix/admin-panel-sync-atomicity`
- **Fix Round 4 base:** `501396c5e2fda0ad515a71855b31bdb0f6a24b3b`
- **Evidence relationship:** this ledger is part of the code/evidence-content commit. The subsequent
  final result-contract commit records that exact parent hash and is the exact HEAD supplied to fresh reviewers.
- **Recorded:** `2026-08-03T02:33:00Z`
- **Plane written:** `false`

Earlier 31/33-row and `0bbcd928` declarations are superseded historical evidence and are not current state.

## Current authoritative inventory

`BEST_EFFORT_ADMIN_PANEL_MUTATIONS == ()`. All 36 rows are `mandatory-sync`.

| Route | Action | Mutation class | Integration path | Transaction owner | Classification |
| --- | --- | --- | --- | --- | --- |
| `update_user_subscription` | `create` | `create` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `extend` | `extend` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `shorten` | `extend` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `set_end_date` | `set_end_date` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `change_tariff` | `change_tariff` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `set_traffic` | `set_traffic` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `cancel` | `cancel` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `reset` | `reset` | `reset_subscription_with_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `activate` | `activate` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `add_traffic` | `set_traffic` | `_sync_subscription_to_panel + enable_remnawave_user` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `remove_traffic` | `set_traffic` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `update_user_subscription` | `set_device_limit` | `set_devices` | `_sync_subscription_to_panel` | `update_user_subscription` | `mandatory-sync` |
| `delete_user_device` | `delete_device` | `set_devices` | `RemnaWaveService.remove_device` | `delete_user_device` | `mandatory-sync` |
| `reset_user_devices` | `reset_devices` | `reset` | `RemnaWaveService.remove_device` | `reset_user_devices` | `mandatory-sync` |
| `full_delete_user` | `delete_user` | `delete_user` | `UserService.delete_user_account` | `full_delete_user` | `mandatory-sync` |
| `delete_user` | `delete_user` | `delete_user` | `_require_panel_disable_for_subscriptions` | `delete_user` | `mandatory-sync` |
| `reset_user_trial` | `reset_trial` | `delete_subscription` | `wipe_trial_subscriptions` | `reset_user_trial` | `mandatory-sync` |
| `reset_user_subscription` | `reset_subscription` | `reset` | `SubscriptionService.disable_remnawave_user` | `reset_user_subscription` | `mandatory-sync` |
| `disable_user` | `disable` | `disable_user` | `SubscriptionService.disable_remnawave_user` | `disable_user` | `mandatory-sync` |
| `block_user` | `block` | `disable_user` | `UserService.block_user -> SubscriptionService.disable_remnawave_user` | `block_user` | `mandatory-sync` |
| `unblock_user` | `unblock` | `activate` | `UserService.unblock_user -> SubscriptionService.update_remnawave_user` | `unblock_user` | `mandatory-sync` |
| `sync_user_to_panel` | `sync_to_panel` | `sync` | `RemnaWaveService direct API` | `sync_user_to_panel` | `mandatory-sync` |
| `update_user_status` | `status_active` | `activate` | `UserService.unblock_user` | `update_user_status` | `mandatory-sync` |
| `update_user_status` | `status_blocked` | `disable_user` | `UserService.block_user` | `update_user_status` | `mandatory-sync` |
| `update_user_status` | `status_deleted` | `delete_user` | `_require_panel_disable_for_subscriptions` | `update_user_status` | `mandatory-sync` |
| `update_existing_tariff` | `tariff_update_sync_squads` | `set_squads` | `_sync_tariff_squads_atomically` | `update_existing_tariff` | `mandatory-sync` |
| `sync_tariff_squads` | `sync_squads` | `set_squads` | `_sync_tariff_squads_atomically` | `sync_tariff_squads` | `mandatory-sync` |
| `_do_extend_subscription` | `extend_subscription` | `extend` | `_sync_subscription_to_panel` | `_do_extend_subscription` | `mandatory-sync` |
| `_do_cancel_subscription` | `cancel_subscription` | `cancel` | `_sync_subscription_to_panel` | `_do_cancel_subscription` | `mandatory-sync` |
| `_do_activate_subscription` | `activate_subscription` | `activate` | `_sync_subscription_to_panel` | `_do_activate_subscription` | `mandatory-sync` |
| `_do_change_tariff` | `change_tariff` | `change_tariff` | `_sync_subscription_to_panel` | `_do_change_tariff` | `mandatory-sync` |
| `_do_add_traffic` | `add_traffic` | `set_traffic` | `_sync_subscription_to_panel + enable_remnawave_user` | `_do_add_traffic` | `mandatory-sync` |
| `_do_set_devices` | `set_devices` | `set_devices` | `_sync_subscription_to_panel` | `_do_set_devices` | `mandatory-sync` |
| `_do_delete_subscription` | `delete_subscription` | `delete_subscription` | `SubscriptionService.disable_remnawave_user` | `_do_delete_subscription` | `mandatory-sync` |
| `_do_delete_user` | `delete_user` | `delete_user` | `UserService.delete_user_account` | `_do_delete_user` | `mandatory-sync` |
| `_do_grant_subscription` | `grant_subscription` | `create` | `_sync_subscription_to_panel` | `_do_grant_subscription` | `mandatory-sync` |

## Delivered code/evidence relationship

- `tests/cabinet/admin_panel_sync_case_manifest.py` contains distinct direct success/skipped/failed rows;
  no direct/status/tariff outcome table is an alias.
- `tests/cabinet/test_admin_direct_panel_sync_matrix.py` executes all 30 direct rows through the real
  relevant public route or `UserService` boundary and asserts route-specific identity, transaction,
  response, and bounded/redacted diagnostic behavior.
- `tests/cabinet/test_admin_panel_sync_inventory.py` derives exported equality from executed matrices,
  includes a mutation proof that deleting one row from any outcome breaks the actual equality guard,
  and rejects restoration of legacy tariff implementations.
- `app/cabinet/routes/admin_tariffs.py` retains only `_sync_tariff_squads_atomically` and its two
  reviewed public fail-closed callers; `_background_sync_squads` and the unreachable legacy route body are gone.
- `app/cabinet/routes/admin_users.py` removes the shadowing local `settings` import exposed by the new
  real public `sync_user_to_panel` matrix.
- Prior `/status`, tariff single-commit, exact-identity, response, and diagnostic changes remain intact.

## Verification

| Command | Current result |
| --- | --- |
| Focused Task 5 suite, including direct/status/tariff matrices | `270 passed, 44 warnings` |
| Focused changed-files Ruff format/check | passed / `All checks passed!` |
| Full `uv run pytest -q` | baseline collection failure: duplicate `has_had_paid_subscription` argument at `tests/services/test_account_merge_service.py:67`; Task 5 did not modify that file |
| `uv run ruff format --check app tests` | baseline failure: the same parser error plus 11 unrelated files requiring formatting |
| `uv run ruff check app tests` | baseline failure: duplicate-keyword parser errors plus unrelated import-order findings |
| `uv run mypy app` | unavailable: `Failed to spawn: mypy`; project does not declare/install mypy |

## Residuals and source state

- Known R2 distributed-atomicity residuals remain: remote success followed by local commit failure,
  timeout with unknown remote outcome, and irreversible provider operations.
- Focused tests report existing deprecation/runtime warnings; no focused failures occurred.
- `uv.lock` is a pre-existing user-owned modification. It is not staged, committed, reverted, or claimed clean.
- Fresh specification-compliance and quality reviews are still required; this ledger does not self-review.

## Recommended Plane update

`plane_written: false`. Record Task 5 Fix Round 4 as implementation complete and awaiting fresh exact-HEAD
specification-compliance and code-quality gates. Attach this ledger, the final result contract, focused
`270 passed` evidence, baseline repository-check evidence, and the explicit exclusion of `uv.lock`.
