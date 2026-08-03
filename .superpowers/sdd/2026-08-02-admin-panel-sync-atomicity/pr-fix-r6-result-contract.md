# PR Fix R6 Result Contract

- task_id: `BEDOLAGA-PANEL-SYNC-ATOMICITY-PR-FIX-R6`
- agent_id: `K2SO`
- status: `DONE_WITH_CONCERNS`
- timestamp_utc: `2026-08-03T15:40:10Z`
- base: `232719bb124cd8781e73d0a511e8ba569296a704`
- branch: `fix/admin-panel-sync-atomicity`
- pr_url: `https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/pull/3134`

## Root cause and fix

`AsyncSession.rollback()` expires mapped instances. Mandatory non-bulk failure
handlers then read mapped ids/relationships for diagnostics (or restored mapped
state), raising `MissingGreenlet` instead of returning the safe response.

Diagnostics now accept immutable subscription-id tuples. Unified reset and
add-traffic, deleted status, standalone delete/trial reset/subscription
reset/disable, full delete, and direct sync snapshot ids before rollback.
Block uses a local subscription id; unblock restores mock-only scalar state
before rollback and logs its pre-snapshotted id after rollback.

## Files changed

- `app/cabinet/routes/admin_users.py`
- `app/services/user_service.py`
- `tests/cabinet/test_admin_panel_sync_contract.py`

## RED to GREEN

The new scalar-diagnostic regression first failed with
`TypeError: unexpected keyword argument 'subscription_ids'`, then passed after
the scalar-only contract was implemented.

## Verification

- Targeted route/service matrix: `191 passed, 32 warnings`.
- Full focused 12-suite atomicity command: `344 passed, 50 warnings`.
- Changed Python paths: Ruff format/check clean; compileall clean.
- `git diff --check`: clean.

## Concerns

The requested real `sqlite+aiosqlite` regressions for every listed family were
not added in this bounded repair; the existing real-session coverage remains in
the bulk suite. Fresh independent review is required; no approval is asserted.
