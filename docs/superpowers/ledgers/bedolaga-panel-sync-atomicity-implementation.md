# Admin Panel Sync Atomicity — Implementation Evidence

- **Task ID:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-T5`
- **Approved specification:** R2, `docs/superpowers/specs/2026-08-02-admin-panel-sync-atomicity-design.md`
- **Specification blob:** `34604d4d797594333bb1411d3ef0a15aeb7e6d8a`
- **Branch:** `fix/admin-panel-sync-atomicity`
- **Base HEAD:** `6acdaf28c3d00533197f88e4f894e66e6b36de06`
- **Reviewed implementation HEAD:** `0bbcd928a828aaae087965d7184ccc240a90d74f` (`docs: record admin panel sync verification`)
- **Recorded:** `2026-08-03T01:45:49Z`
- **Plane written:** `false`

## Inventory

`BEST_EFFORT_ADMIN_PANEL_MUTATIONS` is empty. The 31 executable coverage rows below are all `mandatory-sync`.

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
| `_do_extend_subscription` | `extend_subscription` | `extend` | `_sync_subscription_to_panel` | `_do_extend_subscription` | `mandatory-sync` |
| `_do_cancel_subscription` | `cancel_subscription` | `cancel` | `_sync_subscription_to_panel` | `_do_cancel_subscription` | `mandatory-sync` |
| `_do_activate_subscription` | `activate_subscription` | `activate` | `_sync_subscription_to_panel` | `_do_activate_subscription` | `mandatory-sync` |
| `_do_change_tariff` | `change_tariff` | `change_tariff` | `_sync_subscription_to_panel` | `_do_change_tariff` | `mandatory-sync` |
| `_do_add_traffic` | `add_traffic` | `set_traffic` | `_sync_subscription_to_panel + enable_remnawave_user` | `_do_add_traffic` | `mandatory-sync` |
| `_do_set_devices` | `set_devices` | `set_devices` | `_sync_subscription_to_panel` | `_do_set_devices` | `mandatory-sync` |
| `_do_delete_subscription` | `delete_subscription` | `delete_subscription` | `SubscriptionService.disable_remnawave_user` | `_do_delete_subscription` | `mandatory-sync` |
| `_do_delete_user` | `delete_user` | `delete_user` | `UserService.delete_user_account` | `_do_delete_user` | `mandatory-sync` |
| `_do_grant_subscription` | `grant_subscription` | `create` | `_sync_subscription_to_panel` | `_do_grant_subscription` | `mandatory-sync` |

## Changed files

- `app/services/admin_panel_sync.py` — add the discovered public `delete_user:delete_user` mandatory mutation.
- `tests/cabinet/test_admin_panel_sync_inventory.py` — join single/bulk parameterized case-key sets and ignore private helpers in route discovery.
- `tests/cabinet/test_admin_panel_sync_contract.py` — export and execute single-route case tables.
- `tests/cabinet/test_admin_bulk_panel_sync_contract.py` — derive bulk keys from real handler parameter rows.
- This ledger and the task report.

## Acceptance evidence

- Inventory equality asserts every mandatory key has success, skipped, and failed coverage from the single/bulk executed parameter tables.
- The formerly undisclosed public `delete_user` panel mutation is inventory-classified; private transaction helpers are not represented as routes.
- Existing focused contracts retain exact-subscription targeting: multi-tariff tests assert `subscription.remnawave_uuid`/each exact subscription and reject user-level UUID substitution.
- Structured failure-log tests assert `user_id`, exact `subscription_id`, action, bounded reason code, and exclude credential-like values. No secrets, payloads, or credential URLs are recorded here.

## Verification

| Command | Result |
| --- | --- |
| Focused admin synchronization pytest command from Task 5 | `226 passed, 44 warnings` |
| `uv run ruff format --check app tests` | baseline failure: parser stops on pre-existing duplicate keywords in `tests/services/test_account_merge_service.py`; it also reports unrelated format drift. Task 5 files were formatted and their scoped check passes. |
| `uv run ruff check app tests` | baseline failure: the same duplicate-keyword syntax errors plus unrelated import-order findings. Scoped Task 5 files: `All checks passed!` |
| `uv run mypy app` | unavailable baseline: uv environment has no `mypy` executable; `pyproject.toml` does not declare it. |

## Skipped checks and residual risks

- No full mypy run was possible because the required executable is absent; no focused mypy alternative exists in repository configuration.
- Full repository format/lint cannot pass until unrelated parser/format/import-order baseline issues are resolved; Task 5 did not modify those files.
- The known `uv.lock` modification is user-owned, excluded from staging and commit, and must remain distinguishable from Task 5 cleanliness.
- Accepted R2 residuals remain: remote success followed by database-commit failure, partial remote multi-call work or timeout with unknown outcome, and irreversible provider cancellation/reset/tariff effects. This change does not claim distributed atomicity.

## Recommended Plane update

`plane_written: false`. Record Task 5 as **implementation complete, awaiting fresh specification-compliance and code-quality review** on the committed head; attach this ledger, focused test result, and the repository-baseline check blockers above. Note the explicit `delete_user:delete_user` inventory correction and empty best-effort list.
