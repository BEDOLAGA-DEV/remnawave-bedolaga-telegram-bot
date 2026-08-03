# PR Fix R5 Result Contract

- status: `DONE`
- task_id: `BEDOLAGA-PANEL-SYNC-ATOMICITY-PR-FIX-R5`
- agent_id: `K2SO`
- platform: `Web/full-stack`
- timestamp_utc: `2026-08-03T13:39:42Z`
- prior_head: `7e9419f4e825b022187e98dadcef36f53ad0ed22`
- implementation_commit: `253247e614a8aecd6346069f0e5c912a9e1cfa4e`
- implementation_head: `253247e614a8aecd6346069f0e5c912a9e1cfa4e`
- current_head: the commit containing this contract; resolve with `git rev-parse HEAD`
  after the documentation commit.

## Implementation summary

Resolved the sole R4 Important `MissingGreenlet` regression in both bulk
typed-failure executors. The user-target executor now reads the typed exact
sibling `subscription_id` before rollback, resolves a fallback target only
when that typed scalar is absent, and snapshots the fallback id before
rollback. The subscription-target executor snapshots both `user.id` and the
typed failure `subscription_id` before rollback. Neither typed failure path
dereferences mapped ORM state after `AsyncSession.rollback()`.

Added two real `sqlite+aiosqlite` `AsyncSession` regressions backed by mapped
user/subscription relationships. Each stages a local mutation, raises
`PanelSyncFailed(PANEL_API_FAILED, subscription_id=202)`, executes the real
bulk dispatcher, wraps the real session commit/rollback methods for exact
counts, and records any `do_orm_execute` after rollback.

## Files changed

- `app/cabinet/routes/admin_bulk_actions.py`
- `tests/cabinet/test_admin_bulk_panel_sync_contract.py`
- `.superpowers/sdd/2026-08-02-admin-panel-sync-atomicity/pr-fix-r5-result-contract.md`

No dependency, migration, configuration, model, deployment, release, Plane,
Apple, Android, or unrelated product-area change is included.

## Finding disposition and evidence

### R4 Important — typed bulk failures access expired ORM state after rollback

- status: `RESOLVED`, pending fresh independent exact-head review.
- root cause evidence: before the production fix, the two real-session tests
  failed deterministically with `sqlalchemy.exc.MissingGreenlet`. The user
  stack terminated at `_execute_for_user -> _resolve_subscription ->
  user.subscriptions`; the subscription stack terminated at
  `_execute_for_subscription -> user.id`.
- user-target fix: snapshot `error.subscription_id`; only if it is `None`,
  resolve the mapped target and snapshot its scalar id before rollback. The
  result prefers the typed id and uses only the pre-rollback fallback scalar.
- subscription-target fix: snapshot `user.id` and `error.subscription_id`
  before rollback; construct the safe result entirely from those scalars and
  the requested `sub_id` fallback.
- real-session verification per executor: `rollback == 1`, `commit == 0`,
  `user_id == 42`, `subscription_id == 202`, `success is False`, exact safe
  public failure message, exactly one bounded structured warning, and an empty
  post-rollback `do_orm_execute` list.
- mutation protection: restoring either removed post-rollback dereference
  makes its corresponding real-session test fail with `MissingGreenlet` before
  a result or warning is returned.

## TDD evidence

```text
RED, exact new real-session regressions before production change:
  2 failed, 67 deselected, 28 warnings
  both failures: sqlalchemy.exc.MissingGreenlet

GREEN, exact new real-session regressions after minimal scalar snapshots:
  2 passed, 67 deselected, 28 warnings

whole bulk contract:
  69 passed, 28 warnings
```

The new tests exercise the real SQLAlchemy session/rollback-expiration
behavior. Mocks are confined to external dispatcher/handler/logging edges and
spies that wrap the real `AsyncSession.commit()` and `rollback()` methods.

## Fresh verification

```text
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  tests/cabinet/test_admin_bulk_panel_sync_contract.py \
  -k 'typed_sibling_failure_avoids_real_session_post_rollback_orm_access'
  2 passed, 67 deselected, 28 warnings (exit 0)

PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  tests/cabinet/test_admin_bulk_panel_sync_contract.py
  69 passed, 28 warnings (exit 0)

PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  <12 focused atomicity suites>
  344 passed, 50 warnings in 10.99s (exit 0)

uv run --frozen ruff format --check <22 full-branch changed Python files>
  22 files already formatted (exit 0)
uv run --frozen ruff check <22 full-branch changed Python files>
  All checks passed! (exit 0)
fresh ast.parse check <22 full-branch changed Python files>
  AST syntax parsed: 22 files (exit 0)
PYTHONDONTWRITEBYTECODE=1 uv run --frozen python -m compileall -q \
  <22 full-branch changed Python files>
  exit 0

git diff --check 7e9419f4e825b022187e98dadcef36f53ad0ed22
git diff --check 232719bb124cd8781e73d0a511e8ba569296a704
  exit 0 (both)

R5 exact-path scope guard before this contract:
  app/cabinet/routes/admin_bulk_actions.py
  tests/cabinet/test_admin_bulk_panel_sync_contract.py

boundary-aware full-branch high-risk secret scan:
  no matches
R5 generic credential-assignment scan:
  no matches
```

The first boundary-less `sk-` scan produced false positives from the `sk-`
substring inside existing `task-*` artifact names. A filename/hash-only
classification exposed no values, and the corrected token-boundary scan
returned no matches. The 50 focused-suite warnings are the already recorded
SQLAlchemy/Pydantic deprecations and known payment-test unawaited-`AsyncMock`
warnings; no focused test failed.

## Git and PR evidence

- base_head: `232719bb124cd8781e73d0a511e8ba569296a704`
- branch: `fix/admin-panel-sync-atomicity-r1`
- remote_branch: `origin/fix/admin-panel-sync-atomicity`
- pushed: `true` for the implementation head; the contract commit is pushed
  immediately after this artifact is created.
- implementation remote head:
  `253247e614a8aecd6346069f0e5c912a9e1cfa4e`
- pr_url:
  `https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/pull/3134`
- PR at implementation head: `OPEN`, non-draft, base `main`, head
  `253247e614a8aecd6346069f0e5c912a9e1cfa4e`.
- CI snapshot at implementation head: `pip-audit (dependencies)` completed
  `SUCCESS`; build, build-and-push, CodeQL analysis, and lint were
  `IN_PROGRESS` after the new push.
- working_tree: clean after the contract commit.

## Risks, concerns, and next gate

- blockers: none in the bounded R5 implementation.
- known concerns: fresh independent specification-compliance, code-quality,
  and Chewbacca actual-PR review have not yet run on the contract-containing
  head; this contract does not self-approve those gates.
- verification caveat: full-repository pytest and mypy were outside this
  bounded continuation and were not run.
- accepted R2 architectural risks remain: remote success followed by local
  commit failure, partial multi-call remote success, timeout ambiguity, and
  irreversible payment-provider effects.
- circuit breaker: R5 is the final allowed continuation. Any remaining
  load-bearing finding must block and escalate rather than start another fix
  loop.
- review_gate: dispatch fresh exact-head specification-compliance and
  code-quality reviews, then fresh Chewbacca actual-PR review.
- plane_update_recommendation: record the final contract-containing head, R5
  verification evidence, and pending fresh review gates for PR #3134.
- plane_written: `false`
