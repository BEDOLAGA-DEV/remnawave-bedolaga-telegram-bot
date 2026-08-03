# Task 3 Result Contract — Route Evidence Round 4

- **Task:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-T3`
- **Base:** `82dc2555937dc10b6aa9c40afbe84399c16aec34`

## Contract

| Boundary | Required contract | Evidence |
| --- | --- | --- |
| Unified update route | Every `create`, `extend`, `shorten`, `set_end_date`, `change_tariff`, `set_traffic`, `cancel`, `reset`, `activate`, `add_traffic`, `remove_traffic`, and `set_device_limit` branch fails closed on typed panel outcomes; success uses exact subscription identity and one caller-owned commit. | Real public-route parameter matrix in `test_admin_panel_sync_contract.py`. |
| Full delete | Complete exact panel identities are preflighted before recurring cancellation; route preserves its service response and does not add a second commit. | Service partial-identity regression plus real route response matrix. |
| Trial reset | Strict authoritative wipe deletes every exact panel user before executing local row deletion; route commits once afterward. | Real `wipe_trial_subscriptions` strict-mode order test plus route contract. |
| Device reset | This is remote-only: exact target removal is mandatory and fail-closed; no local DB mutation exists, so it performs **zero commits**. | Device route typed failure and success contracts. |

## Residual

Remote payment cancellation after successful full-delete preflight cannot be rolled back if a later remote/local operation fails. This is the accepted R2 distributed-transaction limitation.
