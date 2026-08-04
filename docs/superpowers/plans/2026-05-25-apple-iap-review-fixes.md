# Apple IAP Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Apple IAP backend production-ready against the review findings without changing unrelated account deletion/OAuth revocation work.

**Architecture:** Keep the existing separation: Pydantic schemas enforce the client contract, Cabinet routes translate fulfillment outcomes to client-safe HTTP semantics, webhook routes translate notification outcomes to Apple-facing HTTP semantics, and `Settings` owns configuration readiness/mounting decisions. Add focused unit/HTTP tests first, then minimal implementation, then one rollback regression where the service currently relies too much on session close behavior.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy async, pytest/anyio, `uv`, Ruff.

---

## Files

- Modify `app/cabinet/schemas/apple_iap.py` for strict purchase payload validation.
- Modify `app/cabinet/apple_iap.py` for purchase result to HTTP status mapping and rollback handling.
- Modify `app/webserver/apple_iap.py` for webhook result to HTTP status mapping and health status.
- Modify `app/config.py` for Apple IAP readiness helpers and route-mount predicate.
- Modify `app/webserver/payments.py`, `app/webserver/unified_app.py`, and `app/webapi/app.py` for partial-config route mounting.
- Modify `tests/external/test_apple_iap.py`, `tests/webserver/test_apple_iap_webhook.py`, and `tests/services/test_apple_iap_service.py` for regression coverage.
- Update `PROJECT_KNOWLEDGE.md` after verification.

## Task 1: Strict Cabinet Purchase Payload

- [ ] Add schema tests proving `ApplePurchaseRequest` rejects forbidden client fields: `amount`, `currency`, `signed_payload`, `receipt`, `user_id`, `environment`, and `app_account_token`.
- [ ] Add schema test proving `product_id` longer than 128 characters is rejected.
- [ ] Run `uv run python -m pytest tests/external/test_apple_iap.py::TestSchemas -q` and verify the new tests fail before implementation.
- [ ] Add `ConfigDict(extra='forbid')` to `ApplePurchaseRequest`.
- [ ] Set `product_id: str = Field(..., min_length=1, max_length=128, description='Apple product ID')`.
- [ ] Keep the existing numeric `transaction_id` validator.
- [ ] Run `uv run python -m pytest tests/external/test_apple_iap.py::TestSchemas -q`.
- [ ] Commit `app/cabinet/schemas/apple_iap.py` and `tests/external/test_apple_iap.py`.

## Task 2: Cabinet Purchase Failure HTTP Semantics

- [ ] Add route tests proving `account_token_mismatch` maps to HTTP 409 and `unknown_product` maps to HTTP 400.
- [ ] Run `uv run python -m pytest tests/external/test_apple_iap.py -q -k 'apple_purchase_maps or account_token_requires'` and verify the new tests fail before implementation.
- [ ] In `app/cabinet/apple_iap.py`, add `_PURCHASE_FAILURE_STATUS_BY_REASON` mapping:
  - `disabled` -> 503
  - `unknown_product`, `verification_failed`, `invalid_transaction`, `validation_failed`, `environment_mismatch` -> 400
  - `account_token_mismatch`, `owner_mismatch`, `duplicate_conflict` -> 409
- [ ] Add `_raise_purchase_failure(result: AppleFulfillmentResult)` that raises `HTTPException` with `detail=result.reason`.
- [ ] In `apple_purchase`, record failure count and call `_raise_purchase_failure(result)` when fulfillment fails; return `ApplePurchaseResponse(success=True)` only on success.
- [ ] Change account-token and purchase disabled/config route guards from HTTP 400 to HTTP 503.
- [ ] Run `uv run python -m pytest tests/external/test_apple_iap.py -q -k 'apple_purchase_maps or account_token_requires or TestSchemas'`.
- [ ] Commit `app/cabinet/apple_iap.py` and `tests/external/test_apple_iap.py`.

## Task 3: Webhook Status Mapping And Health

- [ ] Add webhook tests proving `missing_notification_uuid` and `signed_transaction_verification_failed` map to HTTP 400.
- [ ] Add health test proving `GET /health/apple-iap` returns HTTP 503 with `status=configuration_error` when `APPLE_IAP_ENABLED=true` but config is incomplete.
- [ ] Run `uv run python -m pytest tests/webserver/test_apple_iap_webhook.py -q` and verify the new tests fail before implementation.
- [ ] In `app/webserver/apple_iap.py`, add `_WEBHOOK_REASON_STATUS` mapping for `invalid_signature` -> 403, `configuration_error` -> 503, `missing_notification_uuid` -> 400, and `signed_transaction_verification_failed` -> 400.
- [ ] Replace the existing webhook failure condition chain with a single mapper call.
- [ ] Make health return HTTP 503 only when `APPLE_IAP_ENABLED` is true and `settings.is_apple_iap_enabled()` is false; otherwise return `ok` when enabled and `disabled` when disabled.
- [ ] Run `uv run python -m pytest tests/webserver/test_apple_iap_webhook.py -q`.
- [ ] Commit `app/webserver/apple_iap.py` and `tests/webserver/test_apple_iap_webhook.py`.

## Task 4: Config Readiness And Route Mounting

- [ ] Add settings tests proving invalid `APPLE_IAP_ENVIRONMENT`, empty `APPLE_IAP_PRODUCTS`, and unreadable root certificate paths disable IAP.
- [ ] Add route test proving Cabinet Apple-only routes mount when `APPLE_IAP_ENABLED=true` even if config is incomplete.
- [ ] Add payment router test proving webhook and health routes mount when `APPLE_IAP_ENABLED=true` even if config is incomplete.
- [ ] Run `uv run python -m pytest tests/external/test_apple_iap.py::TestSettings tests/external/test_apple_iap.py::TestAppleIAPRouting tests/webserver/test_apple_iap_webhook.py -q` and verify the new tests fail before implementation.
- [ ] In `Settings`, add `is_apple_iap_environment_valid()`, `should_mount_apple_iap_routes()`, and `get_unreadable_apple_iap_root_cert_paths()`.
- [ ] Update `is_apple_iap_enabled()` to require valid raw environment, non-empty products, and no unreadable cert paths.
- [ ] Replace route registration predicates with `settings.should_mount_apple_iap_routes()` in `app/webserver/payments.py`, `app/webserver/unified_app.py`, and `app/webapi/app.py`.
- [ ] Run the config/route tests again.
- [ ] Commit config and route mounting changes.

## Task 5: Credit Atomicity And Rollback Regression

- [ ] Add `rollback = AsyncMock()` to the service test `_FakeDB`.
- [ ] Add a test proving `fulfill_verified_transaction` rolls back and does not commit when financial transaction creation fails after Apple ledger insert.
- [ ] Run `uv run python -m pytest tests/services/test_apple_iap_service.py -q -k 'rollback or happy_path or insert_race'` and verify the new test fails before implementation.
- [ ] Wrap the credit block from `create_transaction(...)` through `await db.commit()` in `try/except Exception`.
- [ ] On exception, call `await db.rollback()`, log `Apple IAP purchase credit failed before commit`, and re-raise.
- [ ] Keep side effects after the successful commit.
- [ ] Run the selected service tests again.
- [ ] Commit service rollback changes.

## Task 6: Verification And Knowledge Update

- [ ] Run `uv run python -m pytest tests/external/test_apple_iap.py tests/services/test_apple_iap_service.py tests/services/test_apple_iap_reconciliation_service.py tests/webserver/test_apple_iap_webhook.py -q`.
- [ ] Run `uv run ruff format` on edited Python files.
- [ ] Run `uv run ruff check` on edited Python files.
- [ ] Prepend a dated `PROJECT_KNOWLEDGE.md` entry with exact verification commands and remaining sandbox/TestFlight manual QA risk.
- [ ] Commit `PROJECT_KNOWLEDGE.md`.

