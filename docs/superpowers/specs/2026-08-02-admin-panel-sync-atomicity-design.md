# Admin Mutation and RemnaWave Sync Atomicity

**Revision:** R2
**Date:** 2026-08-02
**Status:** Approved by Егор in Telegram on 2026-08-02

## Problem

`_sync_subscription_to_panel` can fail closed by returning `skipped` or
`error`, but admin mutation callers currently ignore that result and may
return `success=True`. An administrator can therefore receive a message such
as “subscription extended by 30 days” even though RemnaWave was not updated.
Logging the identifiers makes the mismatch diagnosable but does not correct
the false-success contract or the divergent state.

## Objective

Make RemnaWave synchronization a blocking part of every admin mutation for
which panel state is required. A known skipped or failed sync must roll back
the local database mutation and return a safe failure response.

## Scope

- Inventory every admin route/action that creates, updates, resets, disables,
  cancels, changes tariff for, or deletes panel-relevant subscription state,
  regardless of whether it uses `_sync_subscription_to_panel`, another
  service, or a direct RemnaWave call.
- The inventory explicitly includes reset, single and bulk delete,
  cancellation, tariff-change, and direct-service/direct-panel paths.
- Classify each inventoried route/action as mandatory-sync or explicitly
  best-effort.
- Treat every inventoried route/action as mandatory-sync unless this
  specification or an approved follow-up explicitly justifies best-effort
  behavior.
- Remove transaction ownership from `_sync_subscription_to_panel`.
- Give skipped and failed sync outcomes an explicit typed contract.
- Make mandatory-sync callers commit only after panel sync succeeds.
- Roll back local database changes on known skipped or failed panel sync.
- Return `success=False` without a false success message.
- Emit structured diagnostics containing `user_id`, `subscription_id`, the
  admin action, and a classified reason.
- Preserve exact-subscription targeting in multi-tariff mode.

## Out of Scope

- A durable outbox or retry worker.
- New synchronization-state tables or UI.
- Distributed transactions with RemnaWave.
- Automatic compensation when RemnaWave succeeds but the subsequent database
  commit fails.
- Unrelated refactoring of admin routes or subscription behavior.

## Design

### Synchronization contract

`_sync_subscription_to_panel` must not call `commit`. It may flush when needed
to obtain database-generated values, but transaction completion remains the
caller's responsibility.

The helper must distinguish:

- success: panel state was created or updated as required;
- skipped: synchronization was not attempted, including missing RemnaWave
  configuration;
- failed: the panel API call or required synchronization work failed.

Skipped and failed outcomes on mandatory-sync paths must be represented by
typed exceptions (for example, `PanelSyncSkipped` and `PanelSyncFailed`) or an
equally explicit typed result that the shared transaction boundary converts
to those failure classes. An untyped dictionary that callers can silently
ignore is not acceptable.

### Transaction boundary

Each mandatory admin mutation performs the following ordered flow:

1. Start or join the route's managed database transaction.
2. Apply the local mutation without committing it.
3. Synchronize the exact target subscription to RemnaWave.
4. If synchronization succeeds, commit once and return `success=True`.
5. If synchronization is skipped or fails, roll back the local transaction
   and return `success=False`.

Shared services called by these routes must not commit before step 4. If an
existing service currently commits internally, the implementation must add a
transaction-safe mode or refactor that boundary without changing unrelated
callers.

This guarantees rollback of the bot database for known panel failures. It
does not claim true atomicity across PostgreSQL, RemnaWave, and payment
providers. A database commit failure after a successful panel update, a
multi-call panel operation that partially succeeds before a later required
call fails, or a timeout with an unknown remote outcome can leave remote and
local state divergent. Irreversible payment-provider effects already
performed by cancellation, reset, or tariff-change flows cannot be rolled
back with the database. These are accepted residual risks and require an
outbox, idempotent reconciliation, or compensation design outside this scope.

### Caller classification

Implementation must produce an enumerated inventory of every admin
route/action that can mutate panel-relevant subscription state. Discovery
must inspect route handlers, called services, bulk operations, and direct
RemnaWave calls; searching only for `_sync_subscription_to_panel` is
insufficient. The inventory must name the route, action, mutation class,
panel integration path, transaction owner, and classification. Every
inventoried route/action is mandatory-sync. A
best-effort exception is permitted only when all of the following are true:

- local completion without panel completion is an intentional product rule;
- the exact route and rationale are documented in the implementation result;
- the response reports partial completion rather than unconditional success;
- a regression test covers the behavior.

No best-effort exception is approved by this R2 specification.

### Responses and diagnostics

On rollback, the response must:

- report `success=False` (or the route's equivalent non-success status);
- state that the local mutation was not saved because panel synchronization
  did not complete;
- avoid claiming that extension, cancellation, activation, reset, tariff
  change, limit change, or another mutation succeeded;
- avoid exposing internal exception text, credentials, URLs containing
  secrets, or RemnaWave payloads.

Structured logs must include `user_id`, exact `subscription_id`, admin action,
and a bounded reason code. Raw secrets and credential-like values are
forbidden.

## Acceptance Criteria

1. No mandatory admin mutation caller returns `success=True` when panel sync
   is skipped or fails.
2. Local fields and related records changed by that operation are absent or
   restored after rollback.
3. A successful operation performs the expected panel call, commits the local
   mutation once, and preserves its prior success response.
4. Failure responses never contain false success text such as “extended by 30
   days”.
5. Logs for skipped and failed synchronization include `user_id`, exact
   `subscription_id`, action, and classified reason without secrets.
6. Multi-tariff operations always target the exact subscription; they never
   substitute the user-level UUID for a missing subscription identity.
7. The implementation result includes the enumerated route/action inventory,
   including reset, single and bulk delete, cancellation, tariff-change, and
   direct-service/direct-panel paths, plus evidence that no panel-relevant
   admin mutation is absent.
8. The explicitly approved best-effort exception list is empty for R2.

## Verification

- Add an inventory completeness test or equivalent executable check that
  covers every enumerated route/action and fails when a panel-relevant admin
  mutation lacks an explicit classification.
- Add parameterized contract coverage for all mandatory route/actions,
  including reset, single and bulk delete, cancellation, tariff-change, and
  direct-service/direct-panel paths.
- For each mutation class, test sync success: exactly one commit across the
  entire route/action execution, including nested services, and a success
  result.
- Test missing configuration or other skipped sync: rollback and non-success.
- Test panel API failure: rollback and non-success.
- Assert that no false success message is returned after rollback.
- Assert structured diagnostic fields and the absence of credential-like
  values.
- Add or retain a multi-tariff regression test proving exact
  `subscription_id` targeting and no user-UUID substitution.
- Run the focused admin route test suite and the repository's required
  formatting, lint, and static checks.

## Risks

- Some called services may commit internally, so the implementation must find
  and remove or parameterize those premature commits on the mandatory paths.
- Holding a database transaction open during a network call increases lock
  duration; the implementation should keep the mutated read/write set narrow
  and retain existing RemnaWave timeouts.
- A successful panel update followed by a failed database commit, partial
  success in a multi-call panel operation, or a timeout with an unknown
  remote outcome can still diverge. These are accepted residual risks for R2
  and must not be described as proof that the panel is unchanged or as fully
  distributed-atomic behavior.
- Cancellation, reset, and tariff-change flows may already have performed an
  irreversible payment-provider action before a later failure. The local
  transaction must still follow the failure contract, while the external
  irreversibility is documented and surfaced as residual risk rather than
  reported as successful end-to-end completion.
