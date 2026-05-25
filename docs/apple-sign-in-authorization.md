# Sign in with Apple Authorization

This integration adds Sign in with Apple as a cabinet OAuth provider. It is separate from Apple In-App Purchase: IAP keys verify StoreKit transactions, while Sign in with Apple keys authenticate users.

## Backend Configuration

Set these variables to enable the provider:

```env
OAUTH_APPLE_ENABLED=true
OAUTH_APPLE_WEB_CLIENT_ID=com.example.service
OAUTH_APPLE_IOS_CLIENT_ID=com.example.app
OAUTH_APPLE_TEAM_ID=ABCDE12345
OAUTH_APPLE_KEY_ID=ABC123DEFG
OAUTH_APPLE_PRIVATE_KEY_PATH=/run/secrets/apple-signin/AuthKey_ABC123DEFG.p8
```

Both Apple client identifiers are required when the provider is enabled:

- `OAUTH_APPLE_WEB_CLIENT_ID`: Apple Services ID for web / Apple JS popup flows.
- `OAUTH_APPLE_IOS_CLIENT_ID`: native iOS Bundle ID configured for Sign in with Apple.

The backend creates Apple's ES256 `client_secret`, exchanges authorization codes at `https://appleid.apple.com/auth/token`, and verifies Apple `id_token` values against `https://appleid.apple.com/auth/keys`.

## Client Contract

Start the flow with:

```http
GET /cabinet/auth/oauth/apple/authorize?client_type=web
```

Use `client_type=web` for Apple JS/web and `client_type=ios` for native iOS. The selected type is stored in the one-time backend state and controls which Apple client ID is used during token exchange and `id_token.aud` validation.

The response includes `authorize_url`, `state`, `nonce`, and `client_type`. The client must preserve `state` and use the returned `nonce` for the Apple request.

For web, use Apple's official Sign in with Apple JS (`appleid.auth.js`) and open the returned `authorize_url`.

For native iOS, use `AuthenticationServices` / `ASAuthorizationAppleIDProvider` directly. The backend returns `authorize_url: null` for `client_type=ios`; native clients should use the returned `state` and send the SHA-256 value of the backend `nonce` to Apple, then post the resulting authorization code to the backend.

Complete login with:

```http
POST /cabinet/auth/oauth/apple/callback
Content-Type: application/json
```

```json
{
  "code": "authorization-code",
  "state": "state-from-authorize",
  "user": {
    "name": {
      "firstName": "Alice",
      "lastName": "Appleseed"
    },
    "email": "alice@example.com"
  }
}
```

`user` is only returned by Apple on the first authorization. Send it when available so the backend can store the name. The backend ignores `user.email` for identity and linking; email is read only from Apple's signed server token response `id_token`.

The backend does not trust the optional client-provided `id_token` as authoritative identity. It exchanges the authorization code with Apple and requires the token endpoint response to contain an `id_token`. The selected `client_type` from Redis state determines whether `redirect_uri` is included (`web`) or omitted (`ios`) during token exchange.

Account linking uses the same provider:

```http
GET /cabinet/auth/account/link/apple/init?client_type=web
POST /cabinet/auth/account/link/apple/callback
```

The callback body is the same shape as the login callback. As with login, `authorize_url` is only returned for web linking; native iOS linking receives `state`, `nonce`, and `client_type` and should complete authorization through `AuthenticationServices`.

## Apple Developer Setup

- Enable Sign in with Apple for the app identifier or Services ID.
- Register the return URL used by the cabinet frontend. The backend provider builds `{CABINET_URL}/auth/oauth/callback`.
- Create a Sign in with Apple `.p8` key and configure `OAUTH_APPLE_TEAM_ID`, `OAUTH_APPLE_KEY_ID`, and private key path.
- Keep the Sign in with Apple key separate from App Store Connect / IAP keys.
