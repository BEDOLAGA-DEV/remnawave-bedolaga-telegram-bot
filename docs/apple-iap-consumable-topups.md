# Apple IAP Consumable Top-Ups

This integration treats Apple In-App Purchase as a payment rail for balance top-ups. Apple is authoritative for StoreKit transaction validity, while the bot remains authoritative for internal balance, subscription purchases, refunds, and abuse controls.

## Product Model

- App Store Connect products must be consumable in-app purchases.
- Each Apple product ID must be present in `APPLE_IAP_PRODUCTS`.
- The backend credits the kopeks amount from `APPLE_IAP_PRODUCTS`; client-provided amount data is ignored.
- Internal subscriptions are still bought from `users.balance_kopeks` through the existing bot/cabinet flows.

## iOS Contract

See [`apple-iap-ios-requirements.md`](apple-iap-ios-requirements.md) for the full iOS implementation checklist.

1. The authenticated app requests `GET /cabinet/apple-iap/account-token`.
2. The app passes the returned UUID to StoreKit as `appAccountToken` before purchase.
3. After StoreKit completes, the app sends `POST /cabinet/apple-purchase` with `transaction_id` and `product_id`.
4. The backend verifies the transaction through App Store Server API, verifies Apple signed data, checks ownership, and credits balance exactly once.
5. The app should finish the StoreKit transaction only after the backend returns success.

## Server Notifications

Configure App Store Server Notifications V2 to send to:

```text
WEBHOOK_URL + APPLE_IAP_WEBHOOK_PATH
```

The webhook persists each notification by `notificationUUID`, verifies the signed payload with Apple's server library, and handles recovery/refund flows idempotently.

## Security Requirements

- Store the App Store Connect `.p8` key in a secret manager or mounted secret file.
- Mount Apple root certificates from a controlled path and configure `APPLE_IAP_ROOT_CERTS_PATHS`.
- Set `APPLE_IAP_APP_APPLE_ID` for production signed data verification.
- Do not log raw signed payloads, private keys, generated JWTs, Authorization headers, or full request bodies.
- Use HTTPS for the cabinet and Apple notification endpoints.

## Environments

- `APPLE_IAP_ENVIRONMENT=Sandbox` is for local/TestFlight validation.
- `APPLE_IAP_ENVIRONMENT=Production` accepts production transactions and may record sandbox App Review transactions without crediting real balance.
- Separate sandbox and production App Store notification URLs should be validated before rollout.

## Refund Policy

Refund notifications debit the credited balance up to the current available balance and record an internal refund transaction. If refunded funds have already been spent, the account is flagged by the refund workflow and active subscriptions may be disabled according to the existing project policy.
