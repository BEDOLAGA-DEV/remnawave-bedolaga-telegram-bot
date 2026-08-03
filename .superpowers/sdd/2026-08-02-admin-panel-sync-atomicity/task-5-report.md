# Task 5 Report — Current Fix Round 4 Evidence

- **Task ID:** `BEDOLAGA-PANEL-SYNC-ATOMICITY-T5-FIX-R4`
- **Base HEAD:** `501396c5e2fda0ad515a71855b31bdb0f6a24b3b`
- **Status:** `CODE_AND_EVIDENCE_CONTENT_READY_FOR_PINNING`
- **Timestamp:** `2026-08-03T02:33:00Z`
- **Plane written:** `false`

The former `0bbcd928`/31-row/33-row and `NOT_REVIEW_READY` declarations are superseded historical
states. They are not the current delivered state and do not claim review of this revision.

## Fix Round 4 result

1. Distinct direct SUCCESS/SKIPPED/FAILED matrices now drive 30 executable parametrizations covering
   all ten direct keys. Every row calls its actual public route or `UserService` contract and checks the
   relevant exact identity, transaction, response, and bounded/redacted diagnostic behavior. Exported
   key sets derive from these executed rows. A mutation test proves removing one row from any outcome
   makes the real 36-row inventory equality fail.
2. Removed the uncalled `_background_sync_squads`, obsolete constants/import, and unreachable legacy
   `sync_tariff_squads` implementation. The two public tariff flows retain only the reviewed
   `_sync_tariff_squads_atomically` fail-closed boundary.
3. Refreshed the implementation ledger to the complete 36-row mandatory inventory, including all
   `/status` and tariff rows, current verification, residuals, and `plane_written: false`.

The real direct-sync parametrization exposed a shadowing local `settings` import that raised
`UnboundLocalError` before the public route could execute; removing that redundant local import is the
minimal code correction required by the matrix contract.

## Verification recorded before the content commit

- Focused Task 5 command including all direct/status/tariff matrices: `270 passed, 44 warnings`.
- Focused changed-files Ruff format/check: passed / `All checks passed!`.
- Full pytest attempt: baseline collection failure at
  `tests/services/test_account_merge_service.py:67` (duplicate function argument); unchanged by Task 5.
- Repository Ruff format attempt: baseline parser failure plus 11 unrelated files requiring formatting.
- Repository Ruff check attempt: baseline parser failures plus unrelated import-order findings.
- Mypy attempt: unavailable, `Failed to spawn: mypy`.
- `uv.lock`: pre-existing user-owned modification, excluded from staging and commits.

## Evidence relationship

This file and the current ledger are included in the code/evidence-content commit. A second, separate
final result-contract commit will append the exact delivered-code parent hash and describe its own
parent/evidence relationship. That final artifact will not claim Chewbacca review or encode a false
self-hash; reviewers must use the exact Git HEAD supplied by the controller.

## Required next gate

Fresh isolated specification-compliance and code-quality reviews are required on the final result-contract
HEAD. No review approval is claimed here.
