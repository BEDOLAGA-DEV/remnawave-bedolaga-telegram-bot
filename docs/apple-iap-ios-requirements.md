# Apple IAP iOS Implementation Requirements

This document is the implementation contract for Apple consumable in-app purchases on iOS. Apple IAP is used only to top up the user's internal balance. The backend is authoritative for balance crediting, refunds, abuse checks, and idempotency. The iOS app is responsible for creating a valid StoreKit 2 purchase, attaching the backend account token, delivering the transaction ID to the backend, and finishing the StoreKit transaction only after backend delivery succeeds.

## Scope

The iOS implementation must support:

- StoreKit 2 consumable product loading.
- Authenticated backend account-token lookup.
- Purchase with StoreKit `.appAccountToken(...)`.
- Backend delivery of verified StoreKit transactions.
- Safe retry of unfinished transactions.
- App relaunch recovery.
- Account switch and logout safety.
- TestFlight/Sandbox and Production behavior.

The iOS implementation must not:

- Credit balance locally.
- Call Apple's App Store Server API.
- Ship App Store Connect `.p8` keys or issuer/key IDs.
- Send receipt blobs, raw transaction JWS, price, currency, or credit amount to the backend.
- Finish a consumable transaction before backend delivery returns `success=true`.

## Minimum Platform And Frameworks

- Use StoreKit 2.
- Recommended minimum: iOS 15+.
- Use Swift concurrency (`async`/`await`) for StoreKit and API calls.
- The app must have the In-App Purchase capability enabled.
- The app bundle ID must match backend `APPLE_IAP_BUNDLE_ID`.

## Product Configuration

Products must be configured in App Store Connect as consumable in-app purchases.

Backend product IDs are configured in `APPLE_IAP_PRODUCTS` as a JSON mapping:

```json
{
  "com.app.client.topup.100": 10000,
  "com.app.client.topup.300": 30000,
  "com.app.client.topup.500": 50000
}
```

iOS requirements:

- Product IDs in the app must exactly match backend product IDs.
- Product IDs must be loaded from app config, remote config, or a shared constants file. Avoid hardcoding IDs in multiple places.
- Show localized StoreKit display name, description, and price from `Product`.
- Do not calculate credited balance from StoreKit price.
- Do not send StoreKit price, currency, or localized data to the backend.
- Hide a top-up option if StoreKit does not return its product.
- Treat missing products as a configuration error and log a non-sensitive diagnostic event.

Recommended Swift model:

```swift
struct AppleTopUpProduct: Identifiable, Equatable {
    let id: String              // App Store product ID
    let displayOrder: Int
    let fallbackTitle: String
}
```

## Backend API Contract

All cabinet endpoints use the existing authenticated cabinet API session. Use the same auth mechanism already used by the iOS app for cabinet requests, for example `Authorization: Bearer <token>` or the app's existing authenticated cookie/session mechanism.

Use HTTPS only.

### Get Account Token

Call this before starting a StoreKit purchase.

```http
GET /cabinet/apple-iap/account-token
Authorization: Bearer <cabinet-token>
Accept: application/json
```

Success response:

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{
  "app_account_token": "123e4567-e89b-12d3-a456-426614174000"
}
```

Client requirements:

- Parse `app_account_token` as a UUID.
- If UUID parsing fails, block purchase and show a retryable error.
- Cache this token only for the currently authenticated backend user.
- Cache key must include the authenticated user ID or stable backend account ID.
- Clear cached token on logout, token refresh failure, or account switch.
- Never generate this UUID locally.
- Never reuse one user's token for another user.

Failure handling:

- `400`: Apple IAP is disabled or not fully configured. Disable the Apple top-up UI and show "Apple purchases are temporarily unavailable."
- `401`/`403`: user is not authenticated or no longer authorized. Re-authenticate before purchase.
- `429`: back off and retry later.
- `5xx`/network error: show retry UI; do not start purchase.

Recommended Swift response model:

```swift
struct AppleAccountTokenResponse: Decodable {
    let appAccountToken: String

    enum CodingKeys: String, CodingKey {
        case appAccountToken = "app_account_token"
    }
}
```

### Deliver Purchase

Call this only after StoreKit returns a locally verified transaction.

```http
POST /cabinet/apple-purchase
Authorization: Bearer <cabinet-token>
Content-Type: application/json
Accept: application/json
```

Request:

```json
{
  "product_id": "com.app.client.topup.100",
  "transaction_id": "2000000123456789"
}
```

Response:

```json
{
  "success": true
}
```

Client requirements:

- `product_id` must be `transaction.productID`.
- `transaction_id` must be `String(transaction.id)`.
- Do not send amount, price, currency, receipt, raw JWS, user ID, or `appAccountToken`.
- If `success=true`, immediately finish the StoreKit transaction.
- If `success=false`, do not finish. Persist it for retry.
- The backend is idempotent. Retrying the same transaction must be treated as safe.

Recommended Swift request/response models:

```swift
struct ApplePurchaseDeliveryRequest: Encodable {
    let productId: String
    let transactionId: String

    enum CodingKeys: String, CodingKey {
        case productId = "product_id"
        case transactionId = "transaction_id"
    }
}

struct ApplePurchaseDeliveryResponse: Decodable {
    let success: Bool
}
```

Delivery result handling:

- `200` with `success=true`: finish transaction and refresh backend balance.
- `200` with `success=false`: do not finish; retry later.
- `400`: do not finish automatically. Keep pending and surface a support/configuration error.
- `401`/`403`: do not finish. Re-authenticate and retry delivery.
- `429`: do not finish. Retry with exponential backoff.
- `5xx`: do not finish. Retry with exponential backoff.
- Network timeout/offline: do not finish. Retry when network returns.
- JSON parse error: do not finish. Treat as temporary backend/client compatibility failure and retry after app update or support action.

## StoreKit Purchase Flow

The purchase flow must be implemented in this order:

1. Confirm user is authenticated.
2. Load StoreKit products if not already loaded.
3. Fetch or refresh backend `app_account_token`.
4. Convert `app_account_token` to `UUID`.
5. Start StoreKit purchase with `.appAccountToken(uuid)`.
6. Handle StoreKit result.
7. Deliver verified transactions to backend.
8. Finish transaction only after backend `success=true`.
9. Refresh balance from backend.

Recommended purchase pseudocode:

```swift
func purchaseTopUp(product: Product) async {
    guard authSession.isAuthenticated else {
        showLogin()
        return
    }

    let accountToken: UUID
    do {
        accountToken = try await appleIAPAPI.fetchAccountToken()
    } catch {
        showPurchaseUnavailable(error)
        return
    }

    do {
        let result = try await product.purchase(options: [
            .appAccountToken(accountToken)
        ])

        switch result {
        case .success(let verificationResult):
            let transaction = try checkVerified(verificationResult)
            await deliverAndFinish(transaction)

        case .pending:
            showPendingApprovalState()

        case .userCancelled:
            return

        @unknown default:
            showPurchaseUnavailable(nil)
        }
    } catch {
        showPurchaseFailed(error)
    }
}
```

Verification helper:

```swift
func checkVerified<T>(_ result: VerificationResult<T>) throws -> T {
    switch result {
    case .verified(let safe):
        return safe
    case .unverified(_, let error):
        throw error
    }
}
```

Delivery and finish helper:

```swift
func deliverAndFinish(_ transaction: Transaction) async {
    guard isSupportedTopUpProduct(transaction.productID) else {
        // Unknown product. Do not deliver and do not finish automatically.
        recordLocalPurchaseIssue(transaction)
        return
    }

    if let token = transaction.appAccountToken,
       let expected = try? await appleIAPAPI.cachedOrFetchedAccountToken(),
       token != expected {
        // Prevent cross-account delivery.
        recordLocalPurchaseIssue(transaction)
        return
    }

    persistPendingDelivery(transaction)

    do {
        let delivered = try await appleIAPAPI.deliverPurchase(
            productID: transaction.productID,
            transactionID: String(transaction.id)
        )

        if delivered {
            removePendingDelivery(transaction)
            await transaction.finish()
            await balanceService.refreshBalance()
            showTopUpSuccess()
        } else {
            scheduleDeliveryRetry(transaction)
            showDeliveryPending()
        }
    } catch {
        scheduleDeliveryRetry(transaction)
        showDeliveryPending()
    }
}
```

## Transaction Listener Requirements

Start a transaction listener once during app startup, before the user can start purchases.

Recommended app startup:

```swift
final class AppleIAPTransactionObserver {
    private var updatesTask: Task<Void, Never>?

    func start() {
        updatesTask = Task.detached(priority: .background) {
            for await result in Transaction.updates {
                do {
                    let transaction = try checkVerified(result)
                    await AppleIAPCoordinator.shared.handleUpdatedTransaction(transaction)
                } catch {
                    // Unverified transaction. Do not deliver or finish.
                    AppleIAPLogger.logUnverifiedTransaction(error)
                }
            }
        }
    }

    func stop() {
        updatesTask?.cancel()
        updatesTask = nil
    }
}
```

Requirements:

- Listen to `Transaction.updates` for approved Ask-to-Buy purchases and delayed StoreKit results.
- Do not finish transactions inside the listener until backend delivery succeeds.
- The listener must ignore transactions for unsupported product IDs.
- The listener must avoid delivering purchases under the wrong logged-in user.
- The listener must not require the purchase screen to be open.

## Pending Delivery Persistence

The app must persist verified but undelivered consumable transactions.

Persist at minimum:

```swift
struct PendingApplePurchaseDelivery: Codable, Equatable {
    let transactionID: String
    let productID: String
    let appAccountToken: String?
    let backendUserID: String
    let createdAt: Date
    var attemptCount: Int
    var nextRetryAt: Date?
}
```

Storage requirements:

- Store pending deliveries in durable local storage, not only memory.
- Key pending deliveries by `transactionID`.
- Store `backendUserID` or equivalent current account identifier.
- On logout, do not delete pending deliveries blindly. Mark them unavailable until the same backend user logs in again.
- On account switch, only retry pending deliveries for the active backend user.
- Remove pending delivery only after both backend `success=true` and `transaction.finish()` complete.

Retry triggers:

- App launch.
- User login.
- App enters foreground.
- Network becomes reachable.
- `Transaction.updates` emits a transaction.
- Manual user retry from UI.

Retry policy:

- Use exponential backoff for backend delivery failures.
- Suggested schedule: 15 seconds, 1 minute, 5 minutes, 15 minutes, 1 hour, then every 6 hours.
- Retry immediately on explicit user action if authenticated and online.
- Respect `429` by waiting at least 60 seconds before retrying.
- Do not run many concurrent delivery attempts for the same transaction. Use a per-transaction in-flight guard.

## Current Entitlements And Historical Transactions

Consumables are finished after successful delivery and generally do not represent ongoing entitlements.

Requirements:

- Do not use current entitlements to calculate balance.
- Do not replay all historical consumable transactions on every launch.
- Only process:
  - new verified transactions from purchase result,
  - verified transactions from `Transaction.updates`,
  - locally persisted pending deliveries that were not finished.

## Account Switch And Logout Behavior

Account switch is a critical abuse boundary.

Requirements:

- Cache account token per backend user.
- Pending delivery must include backend user ID.
- If active user differs from pending delivery user, do not deliver it.
- If StoreKit transaction has `appAccountToken` and it differs from the active user's backend token, do not deliver it.
- On logout:
  - stop user-initiated purchase UI,
  - keep transaction observer running if app architecture allows,
  - do not deliver pending purchases until a matching user session exists.
- On login:
  - fetch account token,
  - retry only pending deliveries for that backend user.

## UI Requirements

Product list:

- Show top-up products only after StoreKit products are loaded.
- Show localized price using `product.displayPrice`.
- Show backend-independent wording such as "Top up balance".
- Hide unavailable products.

Purchase states:

- Idle: product can be selected.
- Loading products: show skeleton/progress.
- Fetching account token: disable purchase buttons.
- StoreKit sheet active: prevent duplicate taps.
- Pending approval: show Ask-to-Buy/pending message.
- Delivering to backend: show "Completing purchase..." and do not allow another delivery for the same transaction.
- Delivery pending retry: show "Purchase received, balance will update shortly" and provide retry.
- Success: refresh balance and show top-up success.
- Failure before StoreKit charge: show normal error.
- Failure after StoreKit verified transaction: show delivery pending, not failed purchase.

Important copy rule:

- If StoreKit purchase succeeded but backend delivery failed, do not tell the user "purchase failed". The user may have been charged. Say the app is still completing the purchase and will retry.

## Error Handling Matrix

| Situation | Finish StoreKit Transaction | User Message | Retry |
| --- | --- | --- | --- |
| `.success(.verified)` and backend `success=true` | Yes | Success | No |
| `.success(.verified)` and backend `success=false` | No | Completing purchase pending | Yes |
| Backend `401`/`403` | No | Sign in again to complete purchase | Yes after auth |
| Backend `429` | No | Too many attempts, retry later | Yes with backoff |
| Backend `400` | No | Purchase cannot be completed, contact support | Manual/support |
| Backend `5xx` | No | Completing purchase pending | Yes |
| Network timeout/offline | No | Completing purchase pending | Yes |
| `.success(.unverified)` | No | Purchase could not be verified by device | No automatic backend delivery |
| `.pending` | No | Waiting for approval | StoreKit update |
| `.userCancelled` | No | None or cancelled | No |
| Unknown product ID | No | Product unavailable | No automatic backend delivery |
| Account token mismatch | No | Sign in with the purchasing account | Retry only under matching user |

## Backend Balance Refresh

After successful delivery:

- Call the existing balance/profile endpoint used elsewhere in the app.
- Update local UI from backend response only.
- Do not add the top-up amount locally.
- If balance refresh fails after delivery success, finish transaction anyway and show a refresh retry.

## Security Requirements

- Never hardcode App Store Connect API keys in the iOS app.
- Never call App Store Server API from the iOS app.
- Never generate backend `appAccountToken` locally.
- Never trust client-side price or amount for crediting.
- Never log:
  - auth tokens,
  - raw transaction JWS,
  - backend account token,
  - full backend request/response bodies for purchase delivery,
  - personally identifiable account data.
- Logs may include:
  - product ID,
  - transaction ID,
  - high-level result,
  - HTTP status code,
  - retry count.
- All backend calls must use HTTPS.
- Pending delivery storage must not expose auth tokens.

## Environment Requirements

The app flow must be identical in Sandbox/TestFlight and Production.

Requirements:

- TestFlight purchases are sandbox purchases.
- Production App Review may create sandbox transactions while using a production backend.
- Do not implement client-side logic that credits or rejects based only on StoreKit environment.
- Do not add a manual "sandbox mode" toggle for users.
- Backend URL must match the app build environment.
- App Store product IDs should be stable across TestFlight and Production for the same bundle.

## Observability Requirements

Add non-sensitive analytics/log events for:

- Product load started/succeeded/failed.
- Account token fetch succeeded/failed.
- Purchase started.
- StoreKit result: verified, unverified, pending, cancelled.
- Backend delivery started/succeeded/failed.
- Transaction finished.
- Pending delivery persisted.
- Pending delivery retried.
- Account mismatch prevented delivery.

Do not include raw tokens or JWS payloads.

Suggested event fields:

```text
event_name
product_id
transaction_id
http_status
delivery_success
attempt_count
storekit_result
error_category
```

## Required Components

The iOS implementation should contain equivalents of these components:

- `AppleIAPProductStore`: loads and caches StoreKit products.
- `AppleIAPAPI`: calls backend account-token and purchase-delivery endpoints.
- `AppleIAPPurchaseCoordinator`: owns purchase flow and delivery flow.
- `AppleIAPPendingDeliveryStore`: persists pending verified transactions.
- `AppleIAPTransactionObserver`: listens to `Transaction.updates`.
- `BalanceService`: refreshes backend balance after delivery.

## Implementation Acceptance Criteria

An implementation is acceptable only if all of the following are true:

- Purchases cannot start unless the user is authenticated.
- Purchase calls `GET /cabinet/apple-iap/account-token` before StoreKit purchase.
- StoreKit purchase uses `.appAccountToken(...)`.
- Backend delivery uses only `product_id` and `transaction_id`.
- The app finishes StoreKit transactions only after backend `success=true`.
- Verified transactions survive app termination before delivery.
- App relaunch retries pending deliveries.
- Duplicate delivery is safe and does not show duplicate success UI.
- Logout/account switch cannot deliver one user's transaction under another user.
- Balance is refreshed from backend after delivery.
- No local optimistic balance credit exists.
- No App Store Server API credentials exist in the iOS app.
- TestFlight purchase works end-to-end.
- Network interruption after StoreKit success but before backend delivery recovers automatically.

## QA Test Plan

Run these tests before production:

1. Product loading
   - All configured products load from StoreKit.
   - Missing product is hidden and logged.

2. Account token
   - Token endpoint returns valid UUID.
   - Invalid/failed token response prevents purchase.
   - Logout clears cached active-user token.

3. Successful purchase
   - StoreKit purchase uses `appAccountToken`.
   - Backend receives `product_id` and `transaction_id`.
   - Backend returns `success=true`.
   - App finishes transaction.
   - App refreshes balance.

4. Duplicate delivery
   - Send same transaction twice.
   - Backend returns success/idempotent behavior.
   - UI does not show double credit.

5. Network failure after StoreKit success
   - Disable network before backend delivery.
   - App does not finish transaction.
   - Pending delivery is persisted.
   - Relaunch app and restore network.
   - App retries and finishes after backend success.

6. Backend failure
   - Simulate `500`.
   - App keeps transaction pending.
   - Retry succeeds later.

7. Rate limit
   - Simulate `429`.
   - App backs off and does not finish transaction.

8. Auth expiration
   - Simulate `401`.
   - App requires login.
   - App retries pending delivery after login.

9. Account switch
   - Start purchase under user A.
   - Prevent delivery.
   - Switch to user B.
   - App must not deliver A's pending transaction under B.
   - Switch back to user A and verify delivery succeeds.

10. Ask-to-Buy or pending
    - StoreKit returns `.pending`.
    - App does not call backend.
    - Later `Transaction.updates` emits verified transaction.
    - App delivers and finishes after backend success.

11. Unverified transaction
    - StoreKit returns `.unverified`.
    - App does not call backend.
    - App does not finish transaction automatically.

12. Refund behavior
    - Backend receives refund notification.
    - App refreshes balance on next profile refresh.
    - App does not try to restore consumable balance locally.

13. TestFlight
    - Complete at least one sandbox purchase end-to-end.
    - Confirm backend records expected transaction environment.

14. App Review readiness
    - Production backend can tolerate sandbox App Review transactions according to backend configuration.
    - Purchase UI has clear support path if delivery remains pending.
