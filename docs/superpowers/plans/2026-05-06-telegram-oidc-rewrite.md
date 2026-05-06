# Telegram Cabinet Auth OIDC Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite cabinet `/auth/telegram/*` to align with the modern Telegram Login OIDC spec — add Authorization Code + PKCE flow, nonce binding, and deprecate the legacy Login Widget.

**Architecture:** Layered. Validation primitives live in `app/cabinet/auth/telegram_auth.py` (PKCE helpers, JWKS validator extended with nonce, token-endpoint client). FastAPI routes in `app/cabinet/routes/auth.py` add two new endpoints (`/oidc/init`, `/oidc/callback`), extend the existing popup endpoint with optional nonce, and convert the deprecated widget endpoints to HTTP 410. Reuses existing `OAuthStateCache` (Redis GETDEL), `TokenReplayCache`, `RateLimitCache`. A static HTML test page exercises both flows for manual verification.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, httpx, PyJWT (RS256 + JWKS), Redis (cache), pytest + pytest-asyncio + httpx mock.

**Spec:** [docs/superpowers/specs/2026-05-06-telegram-oidc-rewrite-design.md](../specs/2026-05-06-telegram-oidc-rewrite-design.md)

---

## File Structure

### Created

| Path | Responsibility |
|------|---------------|
| `tests/cabinet/__init__.py` | Empty (package marker). |
| `tests/cabinet/auth/__init__.py` | Empty (package marker). |
| `tests/cabinet/auth/conftest.py` | Pytest fixtures shared by OIDC tests: RSA key pair, JWKS doc, signed `id_token` factory. |
| `tests/cabinet/auth/test_telegram_oidc_pkce.py` | Unit tests for PKCE generators and nonce. |
| `tests/cabinet/auth/test_telegram_oidc_validate.py` | Unit tests for `validate_telegram_oidc_token` (incl. nonce). |
| `tests/cabinet/auth/test_telegram_oidc_exchange.py` | Unit tests for `exchange_authorization_code`. |
| `tests/cabinet/auth/test_telegram_oidc_routes.py` | Integration tests for `/oidc/init`, `/oidc/callback`, popup with nonce, deprecated widget endpoints, linking flow. |
| `app/cabinet/static/__init__.py` | Empty (package marker so directory ships in builds). |
| `app/cabinet/static/telegram-login-test.html` | Manual test harness with two buttons (popup / code) and a JSON output panel. |

### Modified

| Path | Change summary |
|------|----------------|
| `app/config.py` | Add `TELEGRAM_OIDC_REDIRECT_URI: str = ''`. |
| `app/services/system_settings_service.py` | Register `TELEGRAM_OIDC_REDIRECT_URI` (description, format, example). |
| `app/cabinet/auth/telegram_auth.py` | Add `generate_pkce_pair()`, `generate_oidc_nonce()`, `exchange_authorization_code()`. Extend `validate_telegram_oidc_token(id_token, client_id, expected_nonce=None)`. |
| `app/cabinet/auth/__init__.py` | Re-export new helpers. |
| `app/cabinet/schemas/auth.py` | Add `TelegramOIDCInitRequest`, `TelegramOIDCInitResponse`, `TelegramOIDCCallbackRequest`. Extend `TelegramOIDCAuthRequest` with optional `nonce`. |
| `app/cabinet/routes/auth.py` | Replace user-create-from-OIDC-claims block with shared helper `_create_or_login_user_from_oidc_claims()`. Add `oidc_init()` and `oidc_callback()` handlers. Add optional `nonce` argument flow in `auth_telegram_oidc()`. Convert `auth_telegram_widget()` and `link_telegram_widget()` to 410. |
| `app/cabinet/routes/account_linking.py` | Drop widget fields from `LinkTelegramRequest`, add `nonce`. Add `_link_telegram_to_user()` helper. Wire link branch of `oidc_callback`. |
| `app/cabinet/routes/branding.py` | Mark legacy widget as deprecated in `auth_methods` payload; expose `oidc_code_flow_available` boolean derived from `TELEGRAM_OIDC_REDIRECT_URI`. |
| `app/cabinet/dependencies.py` | Add `_optional_cabinet_user` dependency. |
| `app/webserver/unified_app.py` | Mount `app/cabinet/static` at `/cabinet/static`. |

### Deferred (NOT this iteration — release N+2)

- Delete `validate_telegram_login_widget()` and `TelegramWidgetAuthRequest` schema.
- Delete `auth_telegram_widget()` and `link_telegram_widget()` handlers.
- Remove `TELEGRAM_WIDGET_*` settings.

---

## Conventions

- TDD: write the failing test, run it, implement, verify green, commit.
- Pytest path: `pytest tests/cabinet/auth/<file>.py::<test> -v`.
- Commit messages: conventional commits (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`). Co-author trailer per repo convention.
- Never log raw `id_token` or `code_verifier`. Hashes and 8-char prefixes only.
- All new functions are async if they touch I/O (httpx, Redis, DB).
- Imports follow existing absolute-path style (`from app.cabinet.auth.telegram_auth import ...`).

---

## Task 1: Bootstrap test scaffolding

**Files:**
- Create: `tests/cabinet/__init__.py`
- Create: `tests/cabinet/auth/__init__.py`
- Create: `tests/cabinet/auth/conftest.py`

- [ ] **Step 1: Create empty package markers**

```bash
mkdir -p tests/cabinet/auth
: > tests/cabinet/__init__.py
: > tests/cabinet/auth/__init__.py
```

- [ ] **Step 2: Write `conftest.py` with shared fixtures**

```python
# tests/cabinet/auth/conftest.py
"""Shared fixtures for cabinet Telegram OIDC tests."""

from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope='session')
def rsa_key_pair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    """Generate an RSA key pair for signing test id_tokens."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture(scope='session')
def jwks_doc(rsa_key_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]) -> dict[str, Any]:
    """JWKS document containing the test public key with kid='test-kid'."""
    _, public = rsa_key_pair
    jwk = pyjwt.algorithms.RSAAlgorithm.to_jwk(public, as_dict=True)
    jwk['kid'] = 'test-kid'
    jwk['use'] = 'sig'
    jwk['alg'] = 'RS256'
    return {'keys': [jwk]}


@pytest.fixture
def make_id_token(rsa_key_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]):
    """Factory that signs a JWT id_token with the test private key."""
    private, _ = rsa_key_pair
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def _make(
        *,
        client_id: str = '111222333',
        sub: str = '1234567890',
        telegram_id: int = 1234567890,
        nonce: str | None = None,
        exp_offset: int = 600,
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            'iss': 'https://oauth.telegram.org',
            'aud': client_id,
            'sub': sub,
            'id': telegram_id,
            'iat': now,
            'exp': now + exp_offset,
            'name': 'Test User',
            'preferred_username': 'testuser',
        }
        if nonce is not None:
            claims['nonce'] = nonce
        if extra:
            claims.update(extra)
        return pyjwt.encode(claims, pem, algorithm='RS256', headers={'kid': 'test-kid'})

    return _make
```

- [ ] **Step 3: Verify fixtures import without error**

Run: `pytest tests/cabinet/auth/ --collect-only`
Expected: no errors, "no tests ran" or test count of 0.

- [ ] **Step 4: Commit**

```bash
git add tests/cabinet/__init__.py tests/cabinet/auth/__init__.py tests/cabinet/auth/conftest.py
git commit -m "test(cabinet): scaffold OIDC test fixtures"
```

---

## Task 2: Add `TELEGRAM_OIDC_REDIRECT_URI` setting

**Files:**
- Modify: `app/config.py` (around line 397–400)
- Modify: `app/services/system_settings_service.py` (around line 1043–1057)
- Test: `tests/services/test_system_settings_env_priority.py` (existing — extend)

- [ ] **Step 1: Write failing test for default empty value**

Add this test to `tests/services/test_system_settings_env_priority.py`:

```python
def test_telegram_oidc_redirect_uri_default_empty():
    from app.config import settings
    assert settings.TELEGRAM_OIDC_REDIRECT_URI == ''
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/test_system_settings_env_priority.py::test_telegram_oidc_redirect_uri_default_empty -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'TELEGRAM_OIDC_REDIRECT_URI'`.

- [ ] **Step 3: Add the setting in `app/config.py`**

Edit the existing OIDC block (around line 398):

```python
# Telegram Login OIDC (new system via oauth.telegram.org)
TELEGRAM_OIDC_ENABLED: bool = False
TELEGRAM_OIDC_CLIENT_ID: str = ''
TELEGRAM_OIDC_CLIENT_SECRET: str = ''
TELEGRAM_OIDC_REDIRECT_URI: str = ''  # NEW
```

- [ ] **Step 4: Register the setting in `app/services/system_settings_service.py`**

Insert after the existing `TELEGRAM_OIDC_CLIENT_SECRET` registration block:

```python
'TELEGRAM_OIDC_REDIRECT_URI': {
    'description': 'Redirect URI для Authorization Code flow. Должен быть зарегистрирован в BotFather > Bot Settings > Web Login > Allowed URLs.',
    'format': 'Полный HTTPS URL.',
    'example': 'https://cabinet.example.com/auth/telegram/callback',
    'warning': 'Без этого значения работает только popup flow.',
},
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/services/test_system_settings_env_priority.py::test_telegram_oidc_redirect_uri_default_empty -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/services/system_settings_service.py tests/services/test_system_settings_env_priority.py
git commit -m "feat(cabinet): add TELEGRAM_OIDC_REDIRECT_URI setting"
```

---

## Task 3: PKCE and nonce helpers

**Files:**
- Modify: `app/cabinet/auth/telegram_auth.py` (top of file, near other constants)
- Test: `tests/cabinet/auth/test_telegram_oidc_pkce.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/cabinet/auth/test_telegram_oidc_pkce.py
"""PKCE and nonce generator tests."""

import base64
import hashlib
import re


def test_generate_pkce_pair_verifier_length():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    verifier, _ = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128


def test_generate_pkce_pair_verifier_url_safe():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    verifier, _ = generate_pkce_pair()
    assert re.fullmatch(r'[A-Za-z0-9\-._~]+', verifier)


def test_generate_pkce_pair_challenge_matches_verifier():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    verifier, challenge = generate_pkce_pair()
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    expected = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    assert challenge == expected


def test_generate_pkce_pair_unique():
    from app.cabinet.auth.telegram_auth import generate_pkce_pair
    pairs = {generate_pkce_pair()[0] for _ in range(20)}
    assert len(pairs) == 20


def test_generate_oidc_nonce_length_and_alphabet():
    from app.cabinet.auth.telegram_auth import generate_oidc_nonce
    nonce = generate_oidc_nonce()
    assert len(nonce) == 32
    assert re.fullmatch(r'[0-9a-f]{32}', nonce)


def test_generate_oidc_nonce_unique():
    from app.cabinet.auth.telegram_auth import generate_oidc_nonce
    nonces = {generate_oidc_nonce() for _ in range(20)}
    assert len(nonces) == 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_pkce.py -v`
Expected: FAIL with `ImportError: cannot import name 'generate_pkce_pair'`.

- [ ] **Step 3: Implement helpers in `app/cabinet/auth/telegram_auth.py`**

Add `import base64` and `import secrets` to the imports at the top of the file (next to existing `import hashlib` and `import hmac`). Then insert immediately before `_MAX_CLOCK_SKEW_SECONDS`:

```python
def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE (verifier, challenge) pair using S256.

    Verifier is 43–128 url-safe ASCII characters (RFC 7636 unreserved alphabet).
    Challenge is base64url(sha256(verifier)) with padding removed.
    """
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return verifier, challenge


def generate_oidc_nonce() -> str:
    """Generate a random 32-hex nonce for OIDC replay protection."""
    return secrets.token_hex(16)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_pkce.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/auth/telegram_auth.py tests/cabinet/auth/test_telegram_oidc_pkce.py
git commit -m "feat(cabinet): add PKCE and nonce helpers for Telegram OIDC"
```

---

## Task 4: Extend `validate_telegram_oidc_token` with `expected_nonce`

**Files:**
- Modify: `app/cabinet/auth/telegram_auth.py` (around line 225)
- Test: `tests/cabinet/auth/test_telegram_oidc_validate.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/cabinet/auth/test_telegram_oidc_validate.py
"""Tests for validate_telegram_oidc_token nonce handling."""

import pytest


@pytest.mark.asyncio
async def test_validate_oidc_token_no_nonce_required(make_id_token, jwks_doc, monkeypatch):
    """If expected_nonce is None, claims.nonce is ignored even when present."""
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111', nonce='abc123')
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111')
    assert claims is not None
    assert claims['sub'] == '1234567890'


@pytest.mark.asyncio
async def test_validate_oidc_token_nonce_match(make_id_token, jwks_doc, monkeypatch):
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111', nonce='nonce-xyz')
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111', expected_nonce='nonce-xyz')
    assert claims is not None
    assert claims['nonce'] == 'nonce-xyz'


@pytest.mark.asyncio
async def test_validate_oidc_token_nonce_mismatch(make_id_token, jwks_doc, monkeypatch):
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111', nonce='nonce-xyz')
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111', expected_nonce='nonce-other')
    assert claims is None


@pytest.mark.asyncio
async def test_validate_oidc_token_missing_nonce_when_expected(make_id_token, jwks_doc, monkeypatch):
    """If expected_nonce is set but token has no nonce claim, validation fails."""
    from app.cabinet.auth import telegram_auth

    async def _fake_get_jwks(force: bool = False):
        return jwks_doc

    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    token = make_id_token(client_id='111')  # no nonce
    claims = await telegram_auth.validate_telegram_oidc_token(token, '111', expected_nonce='something')
    assert claims is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_validate.py -v`
Expected: FAIL with `TypeError: validate_telegram_oidc_token() got an unexpected keyword argument 'expected_nonce'`.

- [ ] **Step 3: Extend the function in `app/cabinet/auth/telegram_auth.py`**

Replace the existing `validate_telegram_oidc_token` definition (around line 225) with:

```python
async def validate_telegram_oidc_token(
    id_token: str,
    client_id: str,
    expected_nonce: str | None = None,
) -> dict[str, Any] | None:
    """
    Validate a Telegram OIDC id_token using JWKS.

    Args:
        id_token: JWT id_token from Telegram OIDC flow.
        client_id: Expected audience (bot's numeric ID as string).
        expected_nonce: If provided, claims['nonce'] must equal this value.

    Returns:
        Decoded claims dict if valid, None otherwise.
    """
    try:
        jwks_data = await _get_jwks()
        public_keys = _build_public_keys(jwks_data)

        unverified_header = pyjwt.get_unverified_header(id_token)
        kid = unverified_header.get('kid')

        if kid and kid not in public_keys:
            refreshed = await _force_refresh_jwks(kid)
            if refreshed:
                public_keys = _build_public_keys(refreshed)

        if not kid or kid not in public_keys:
            logger.warning('Telegram OIDC: unknown kid in id_token', kid=kid)
            return None

        claims = pyjwt.decode(
            id_token,
            key=public_keys[kid],
            algorithms=['RS256'],
            audience=client_id,
            issuer=_OIDC_ISSUER,
            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']},
        )

        if expected_nonce is not None:
            actual = claims.get('nonce')
            if actual != expected_nonce:
                logger.warning(
                    'Telegram OIDC: nonce mismatch',
                    has_nonce=actual is not None,
                )
                return None

        return claims

    except pyjwt.ExpiredSignatureError:
        logger.warning('Telegram OIDC: id_token expired')
        return None
    except pyjwt.InvalidTokenError as e:
        logger.warning('Telegram OIDC: invalid id_token', error=str(e))
        return None
    except httpx.HTTPError as e:
        logger.error('Telegram OIDC: failed to fetch JWKS', error=str(e))
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_validate.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/auth/telegram_auth.py tests/cabinet/auth/test_telegram_oidc_validate.py
git commit -m "feat(cabinet): nonce binding for Telegram OIDC token validation"
```

---

## Task 5: `exchange_authorization_code()` against Telegram token endpoint

**Files:**
- Modify: `app/cabinet/auth/telegram_auth.py` (after `validate_telegram_oidc_token`)
- Test: `tests/cabinet/auth/test_telegram_oidc_exchange.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/cabinet/auth/test_telegram_oidc_exchange.py
"""Tests for exchange_authorization_code."""

import httpx
import pytest


@pytest.mark.asyncio
async def test_exchange_success(monkeypatch):
    from app.cabinet.auth import telegram_auth

    captured: dict = {}

    class _MockResponse:
        status_code = 200

        def json(self):
            return {
                'id_token': 'fake.jwt.token',
                'access_token': 'access',
                'token_type': 'bearer',
                'expires_in': 3600,
            }

        def raise_for_status(self):
            return None

    class _MockClient:
        def __init__(self, *args, **kwargs):
            captured['client_kwargs'] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, data, headers, auth):
            captured['url'] = url
            captured['data'] = data
            captured['auth'] = auth
            return _MockResponse()

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    token = await telegram_auth.exchange_authorization_code(
        code='auth_code_xyz',
        code_verifier='verifier_abc',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )

    assert token == 'fake.jwt.token'
    assert captured['url'] == 'https://oauth.telegram.org/token'
    assert captured['data']['grant_type'] == 'authorization_code'
    assert captured['data']['code'] == 'auth_code_xyz'
    assert captured['data']['code_verifier'] == 'verifier_abc'
    assert captured['data']['redirect_uri'] == 'https://cab.example.com/cb'
    assert captured['data']['client_id'] == '111'
    assert captured['auth'] == ('111', 'secret')


@pytest.mark.asyncio
async def test_exchange_4xx_returns_none(monkeypatch):
    from app.cabinet.auth import telegram_auth

    class _MockResponse:
        status_code = 400

        def json(self):
            return {'error': 'invalid_grant'}

        def raise_for_status(self):
            raise httpx.HTTPStatusError('400', request=httpx.Request('POST', 'https://x'), response=httpx.Response(400))

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            return _MockResponse()

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    result = await telegram_auth.exchange_authorization_code(
        code='c',
        code_verifier='v',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )
    assert result is None


@pytest.mark.asyncio
async def test_exchange_timeout_returns_none(monkeypatch):
    from app.cabinet.auth import telegram_auth

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            raise httpx.TimeoutException('slow')

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    result = await telegram_auth.exchange_authorization_code(
        code='c',
        code_verifier='v',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )
    assert result is None


@pytest.mark.asyncio
async def test_exchange_no_id_token_in_response(monkeypatch):
    from app.cabinet.auth import telegram_auth

    class _MockResponse:
        status_code = 200

        def json(self):
            return {'access_token': 'access'}  # missing id_token

        def raise_for_status(self):
            return None

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _MockResponse()

    monkeypatch.setattr(telegram_auth.httpx, 'AsyncClient', _MockClient)

    result = await telegram_auth.exchange_authorization_code(
        code='c',
        code_verifier='v',
        redirect_uri='https://cab.example.com/cb',
        client_id='111',
        client_secret='secret',
    )
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_exchange.py -v`
Expected: FAIL with `ImportError: cannot import name 'exchange_authorization_code'`.

- [ ] **Step 3: Implement the function in `app/cabinet/auth/telegram_auth.py`**

Add at the bottom of the file:

```python
_TOKEN_ENDPOINT = 'https://oauth.telegram.org/token'
_TOKEN_ENDPOINT_TIMEOUT_SECONDS = 10


async def exchange_authorization_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> str | None:
    """Exchange an OAuth authorization code for an id_token at Telegram's token endpoint.

    Returns the id_token string on success, or None on any failure (network, 4xx/5xx,
    missing id_token in response).
    """
    proxy = settings.PROXY_URL if hasattr(settings, 'PROXY_URL') and settings.PROXY_URL else None
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': code_verifier,
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    try:
        async with httpx.AsyncClient(timeout=_TOKEN_ENDPOINT_TIMEOUT_SECONDS, proxy=proxy) as client:
            response = await client.post(
                _TOKEN_ENDPOINT,
                data=data,
                headers=headers,
                auth=(client_id, client_secret),
            )
            response.raise_for_status()
            payload = response.json()
            id_token = payload.get('id_token')
            if not isinstance(id_token, str) or not id_token:
                logger.warning('Telegram OIDC: token endpoint response missing id_token')
                return None
            return id_token
    except httpx.HTTPStatusError as exc:
        logger.warning(
            'Telegram OIDC: token exchange rejected',
            status=exc.response.status_code if exc.response is not None else None,
        )
        return None
    except httpx.TimeoutException:
        logger.error('Telegram OIDC: token exchange timeout')
        return None
    except httpx.HTTPError as exc:
        logger.error('Telegram OIDC: token exchange transport error', error=str(exc))
        return None
    except (ValueError, TypeError) as exc:
        logger.error('Telegram OIDC: token endpoint returned non-JSON', error=str(exc))
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_exchange.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/auth/telegram_auth.py tests/cabinet/auth/test_telegram_oidc_exchange.py
git commit -m "feat(cabinet): exchange_authorization_code() for Telegram OIDC token endpoint"
```

---

## Task 6: Re-export new helpers from `auth/__init__.py`

**Files:**
- Modify: `app/cabinet/auth/__init__.py`

- [ ] **Step 1: Read the current exports**

Run: `grep -n "from .telegram_auth" app/cabinet/auth/__init__.py`

- [ ] **Step 2: Add the new exports**

Edit `app/cabinet/auth/__init__.py` to add `exchange_authorization_code`, `generate_pkce_pair`, `generate_oidc_nonce` to the existing `from .telegram_auth import` block:

```python
from .telegram_auth import (
    exchange_authorization_code,
    extract_telegram_user_from_init_data,
    generate_oidc_nonce,
    generate_pkce_pair,
    validate_telegram_init_data,
    validate_telegram_login_widget,
    validate_telegram_oidc_token,
)
```

If `__all__` is defined, add the same three names there.

- [ ] **Step 3: Verify imports succeed**

Run: `python -c "from app.cabinet.auth import exchange_authorization_code, generate_pkce_pair, generate_oidc_nonce; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add app/cabinet/auth/__init__.py
git commit -m "chore(cabinet): export new OIDC helpers"
```

---

## Task 7: New Pydantic schemas for OIDC init/callback

**Files:**
- Modify: `app/cabinet/schemas/auth.py` (replace `TelegramOIDCAuthRequest`, append three new schemas)
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py` (initial schema tests)

- [ ] **Step 1: Write failing tests**

Create `tests/cabinet/auth/test_telegram_oidc_routes.py` (this file accumulates more tests in later tasks):

```python
# tests/cabinet/auth/test_telegram_oidc_routes.py
"""Schema and route tests for the Telegram OIDC rewrite."""

import pytest
from pydantic import ValidationError


def test_init_request_login_mode_no_jwt():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    req = TelegramOIDCInitRequest(mode='login')
    assert req.mode == 'login'
    assert req.campaign_slug is None


def test_init_request_link_mode():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    req = TelegramOIDCInitRequest(mode='link')
    assert req.mode == 'link'


def test_init_request_invalid_mode_rejected():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    with pytest.raises(ValidationError):
        TelegramOIDCInitRequest(mode='register')


def test_init_request_referral_pattern():
    from app.cabinet.schemas.auth import TelegramOIDCInitRequest
    with pytest.raises(ValidationError):
        TelegramOIDCInitRequest(mode='login', referral_code='!!bad!!')


def test_init_response_shape():
    from app.cabinet.schemas.auth import TelegramOIDCInitResponse
    resp = TelegramOIDCInitResponse(
        authorize_url='https://oauth.telegram.org/auth?...',
        state='S' * 64,
        expires_in=600,
    )
    assert resp.expires_in == 600


def test_callback_request_shape():
    from app.cabinet.schemas.auth import TelegramOIDCCallbackRequest
    req = TelegramOIDCCallbackRequest(code='abc', state='S' * 64)
    assert req.code == 'abc'


def test_oidc_auth_request_optional_nonce():
    from app.cabinet.schemas.auth import TelegramOIDCAuthRequest
    req = TelegramOIDCAuthRequest(id_token='x.y.z', nonce='abc123def456')
    assert req.nonce == 'abc123def456'

    req2 = TelegramOIDCAuthRequest(id_token='x.y.z')
    assert req2.nonce is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -v`
Expected: FAIL with `ImportError: cannot import name 'TelegramOIDCInitRequest'`.

- [ ] **Step 3: Update `app/cabinet/schemas/auth.py`**

Add `Literal` to the typing import at the top:

```python
from typing import Literal
```

Replace the existing `TelegramOIDCAuthRequest` (around line 38) with the version that includes `nonce`, then append the three new schemas:

```python
class TelegramOIDCAuthRequest(BaseModel):
    """Request for Telegram OIDC authentication (popup flow)."""

    id_token: str = Field(..., max_length=4096, description='JWT id_token from Telegram OIDC popup')
    nonce: str | None = Field(
        None,
        min_length=8,
        max_length=128,
        pattern=r'^[A-Za-z0-9_\-]+$',
        description='Nonce that was passed to Telegram.Login.open() — must equal claims["nonce"]',
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$', description='Referral code of inviter'
    )


class TelegramOIDCInitRequest(BaseModel):
    """Initiate Authorization Code + PKCE flow."""

    mode: Literal['login', 'link'] = Field(..., description='login (no JWT) or link (JWT required)')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$', description='Referral code of inviter'
    )


class TelegramOIDCInitResponse(BaseModel):
    """Response containing Telegram authorize URL and state."""

    authorize_url: str = Field(..., description='URL the browser should be redirected to')
    state: str = Field(..., min_length=16, max_length=128, description='CSRF state token (echo of stored state)')
    expires_in: int = Field(..., description='State TTL in seconds')


class TelegramOIDCCallbackRequest(BaseModel):
    """Body for the Authorization Code callback."""

    code: str = Field(..., min_length=1, max_length=2048, description='Authorization code from Telegram')
    state: str = Field(..., min_length=16, max_length=128, description='CSRF state token (must match init)')
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/schemas/auth.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): schemas for Telegram OIDC init/callback flow"
```

---

## Task 8: Refactor user-create-from-claims into shared helper

**Files:**
- Modify: `app/cabinet/routes/auth.py`

This is a pure refactor — behavior is unchanged. The current popup handler `auth_telegram_oidc()` (lines 710–853) inlines the OIDC-claims-to-user logic. Extract it so both the popup and code-flow callbacks call the same code.

- [ ] **Step 1: Read the existing implementation**

Open `app/cabinet/routes/auth.py` and locate the block from `# Extract user info from OIDC claims` (around line 764) to the final `return response` (around line 853) inside `auth_telegram_oidc`.

- [ ] **Step 2: Add the shared helper above `auth_telegram_oidc`**

```python
async def _create_or_login_user_from_oidc_claims(
    db: AsyncSession,
    claims: dict,
    *,
    campaign_slug: str | None,
    referral_code: str | None,
) -> AuthResponse:
    """Create or fetch a user from validated Telegram OIDC claims and return AuthResponse.

    Shared between the popup endpoint (`/auth/telegram/oidc`) and the Authorization Code
    callback (`/auth/telegram/oidc/callback`). Handles referral resolution, user creation,
    user-info refresh, refresh-token storage, and campaign-bonus application.
    """
    try:
        telegram_id = int(claims.get('id', claims.get('sub', 0)))
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid user ID in OIDC claims',
        ) from e
    if not telegram_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Missing user ID in OIDC claims',
        )

    first_name = claims.get('name', claims.get('given_name', ''))
    username = claims.get('preferred_username')
    last_name = claims.get('family_name')
    language = claims.get('locale', 'ru')[:2] if claims.get('locale') else 'ru'

    user = await get_user_by_telegram_id(db, telegram_id)

    referrer_id = None
    if referral_code and not user:
        try:
            referrer = await get_user_by_referral_code(db, referral_code)
            if referrer:
                if referrer.telegram_id and referrer.telegram_id == telegram_id:
                    logger.warning(
                        'Self-referral attempt blocked via telegram_id',
                        telegram_id=telegram_id,
                        referral_code=referral_code,
                    )
                else:
                    referrer_id = referrer.id
        except Exception as e:
            logger.warning('Failed to resolve referral code', referral_code=referral_code, error=e)

    is_new_user = not user
    if not user:
        logger.info('Creating new user from cabinet OIDC', telegram_id=telegram_id, username=username)
        user = await create_user(
            db=db,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language=language,
            referred_by_id=referrer_id,
        )
        logger.info('User created successfully', user_id=user.id, telegram_id=user.telegram_id)

    if user.status != UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User account is not active',
        )

    if username and username != user.username:
        user.username = username
    if first_name and first_name != user.first_name:
        user.first_name = first_name
    if last_name is not None and last_name != user.last_name:
        user.last_name = last_name

    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)

    await _process_referral_code(db, user, referral_code, is_new_user=is_new_user)

    if referrer_id and telegram_id:
        try:
            from app.services.referral_service import clear_pending_referral

            await clear_pending_referral(telegram_id)
        except Exception:
            pass

    response.campaign_bonus = await _process_campaign_bonus(db, user, campaign_slug)
    if response.campaign_bonus:
        response.user = _user_to_response(user)

    return response
```

- [ ] **Step 3: Replace the inlined block in `auth_telegram_oidc`**

Replace everything from `# Extract user info from OIDC claims` to the final `return response` inside `auth_telegram_oidc` with:

```python
    return await _create_or_login_user_from_oidc_claims(
        db,
        claims,
        campaign_slug=request.campaign_slug,
        referral_code=request.referral_code,
    )
```

- [ ] **Step 4: Run repo tests to confirm no regression**

Run: `pytest -q -k "auth or oidc"`
Expected: existing tests still pass (no new tests in this task).

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/auth.py
git commit -m "refactor(cabinet): extract OIDC claims-to-user helper"
```

---

## Task 9: Add optional `nonce` to popup `/auth/telegram/oidc`

**Files:**
- Modify: `app/cabinet/routes/auth.py` (the `auth_telegram_oidc` handler)
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Write failing test**

Append:

```python
@pytest.mark.asyncio
async def test_popup_endpoint_passes_nonce_to_validator(monkeypatch):
    """If request.nonce is set, validate_telegram_oidc_token receives expected_nonce."""
    from app.cabinet.routes import auth as auth_routes
    from app.cabinet.schemas.auth import TelegramOIDCAuthRequest

    captured: dict = {}

    async def _fake_validate(id_token, client_id, expected_nonce=None):
        captured['expected_nonce'] = expected_nonce
        return None  # 401 path keeps the test simple

    async def _no_rate_limit(*a, **kw):
        return False

    async def _setting(db, key):
        return {'TELEGRAM_OIDC_ENABLED': 'true', 'TELEGRAM_OIDC_CLIENT_ID': '111'}.get(key)

    monkeypatch.setattr(auth_routes, 'validate_telegram_oidc_token', _fake_validate)
    monkeypatch.setattr(auth_routes.RateLimitCache, 'is_ip_rate_limited', staticmethod(_no_rate_limit))
    monkeypatch.setattr(auth_routes, 'get_setting_value', _setting)

    request_obj = TelegramOIDCAuthRequest(id_token='x.y.z', nonce='nonce-from-frontend')

    class _Req:
        @property
        def headers(self):
            return {}

        client = type('c', (), {'host': '127.0.0.1'})()

    with pytest.raises(Exception):
        await auth_routes.auth_telegram_oidc(request_obj, _Req(), db=None)

    assert captured['expected_nonce'] == 'nonce-from-frontend'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py::test_popup_endpoint_passes_nonce_to_validator -v`
Expected: FAIL — `expected_nonce` is `None`.

- [ ] **Step 3: Update `auth_telegram_oidc` to forward nonce**

Find the call to `validate_telegram_oidc_token(...)` in `auth_telegram_oidc` and replace with:

```python
if request.nonce is None:
    logger.info(
        'Telegram OIDC popup token received without nonce (frontend should add nonce)',
        client_ip=client_ip,
    )

claims = await validate_telegram_oidc_token(
    request.id_token,
    oidc_client_id,
    expected_nonce=request.nonce,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py::test_popup_endpoint_passes_nonce_to_validator -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/auth.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): forward nonce from popup OIDC request to validator"
```

---

## Task 10: `_optional_cabinet_user` dependency + `POST /auth/telegram/oidc/init`

**Files:**
- Modify: `app/cabinet/dependencies.py`
- Modify: `app/cabinet/routes/auth.py`
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Add `_optional_cabinet_user`**

In `app/cabinet/dependencies.py`, append:

```python
async def _optional_cabinet_user(
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
) -> User | None:
    """Return the current user if a JWT was supplied, else None.

    Distinguishes 'no token' (return None) from 'invalid token' (raise 401).
    """
    auth = request.headers.get('authorization')
    if not auth or not auth.lower().startswith('bearer '):
        return None
    return await get_current_cabinet_user(request, db)
```

- [ ] **Step 2: Write failing tests for `oidc_init`**

Append to `tests/cabinet/auth/test_telegram_oidc_routes.py`:

```python
import urllib.parse

from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def app_client(monkeypatch):
    """Bring up the cabinet FastAPI app with OIDC settings stubbed in."""
    from app.webserver.unified_app import create_app
    from app.cabinet.routes import auth as auth_routes

    async def _enabled(db, key):
        return {
            'TELEGRAM_OIDC_ENABLED': 'true',
            'TELEGRAM_OIDC_CLIENT_ID': '111222333',
            'TELEGRAM_OIDC_CLIENT_SECRET': 'secret',
            'TELEGRAM_OIDC_REDIRECT_URI': 'https://cabinet.example.com/auth/telegram/callback',
        }.get(key)

    async def _no_rate_limit(*a, **kw):
        return False

    monkeypatch.setattr(auth_routes, 'get_setting_value', _enabled)
    monkeypatch.setattr(auth_routes.RateLimitCache, 'is_ip_rate_limited', staticmethod(_no_rate_limit))

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        yield client


@pytest.mark.asyncio
async def test_oidc_init_login_returns_authorize_url(app_client, monkeypatch):
    captured: dict = {}

    async def _gen_state(provider, extra_data=None):
        captured['provider'] = provider
        captured['extra_data'] = extra_data
        return 'S' * 64

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'generate_oauth_state', _gen_state)

    response = await app_client.post('/auth/telegram/oidc/init', json={'mode': 'login'})
    assert response.status_code == 200
    body = response.json()
    assert body['state'] == 'S' * 64
    assert body['expires_in'] == 600

    parsed = urllib.parse.urlparse(body['authorize_url'])
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.netloc == 'oauth.telegram.org'
    assert parsed.path == '/auth'
    assert qs['response_type'] == 'code'
    assert qs['scope'] == 'openid profile'
    assert qs['code_challenge_method'] == 'S256'
    assert qs['client_id'] == '111222333'
    assert qs['redirect_uri'] == 'https://cabinet.example.com/auth/telegram/callback'
    assert qs['state'] == 'S' * 64
    assert qs['code_challenge']
    assert qs['nonce']

    assert captured['provider'] == 'telegram'
    assert captured['extra_data']['flow'] == 'login'
    assert captured['extra_data']['code_verifier']
    assert captured['extra_data']['nonce']


@pytest.mark.asyncio
async def test_oidc_init_disabled_returns_400(app_client, monkeypatch):
    async def _disabled(db, key):
        return 'false' if key == 'TELEGRAM_OIDC_ENABLED' else None

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'get_setting_value', _disabled)

    response = await app_client.post('/auth/telegram/oidc/init', json={'mode': 'login'})
    assert response.status_code == 400
    assert 'not configured' in response.json()['detail'].lower()


@pytest.mark.asyncio
async def test_oidc_init_link_requires_jwt(app_client):
    response = await app_client.post('/auth/telegram/oidc/init', json={'mode': 'link'})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oidc_init_no_redirect_uri_returns_400(app_client, monkeypatch):
    async def _missing_redirect(db, key):
        return {'TELEGRAM_OIDC_ENABLED': 'true', 'TELEGRAM_OIDC_CLIENT_ID': '111'}.get(key, '')

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'get_setting_value', _missing_redirect)

    response = await app_client.post('/auth/telegram/oidc/init', json={'mode': 'login'})
    assert response.status_code == 400
    assert 'redirect uri' in response.json()['detail'].lower()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k oidc_init -v`
Expected: 4 failures (404 — endpoint not registered).

- [ ] **Step 4: Implement `oidc_init`**

Add imports near the top of `app/cabinet/routes/auth.py`:

```python
from urllib.parse import urlencode

from ..auth.oauth_providers import generate_oauth_state, validate_oauth_state
from ..auth import (
    exchange_authorization_code,
    generate_oidc_nonce,
    generate_pkce_pair,
)
from ..dependencies import _optional_cabinet_user
```

(Confirm `from ..dependencies import get_cabinet_db, get_current_cabinet_user` already exists; merge the new import.)

Add this handler after `auth_telegram_oidc`:

```python
_OIDC_AUTHORIZE_ENDPOINT = 'https://oauth.telegram.org/auth'
_OIDC_STATE_TTL_SECONDS = 600


async def _resolve_oidc_settings(db: AsyncSession) -> tuple[bool, str, str, str]:
    """Read OIDC settings (DB override → env). Returns (enabled, client_id, client_secret, redirect_uri)."""
    enabled_val = await get_setting_value(db, 'TELEGRAM_OIDC_ENABLED')
    client_id_val = await get_setting_value(db, 'TELEGRAM_OIDC_CLIENT_ID')
    client_secret_val = await get_setting_value(db, 'TELEGRAM_OIDC_CLIENT_SECRET')
    redirect_uri_val = await get_setting_value(db, 'TELEGRAM_OIDC_REDIRECT_URI')

    client_id = client_id_val or settings.TELEGRAM_OIDC_CLIENT_ID
    client_secret = client_secret_val or settings.TELEGRAM_OIDC_CLIENT_SECRET
    redirect_uri = redirect_uri_val or settings.TELEGRAM_OIDC_REDIRECT_URI
    enabled = (
        enabled_val.lower() == 'true' if enabled_val is not None else settings.TELEGRAM_OIDC_ENABLED
    ) and bool(client_id)

    return enabled, client_id, client_secret, redirect_uri


@router.post('/telegram/oidc/init', response_model=TelegramOIDCInitResponse)
async def oidc_init(
    request: TelegramOIDCInitRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
    user: User | None = Depends(_optional_cabinet_user),
):
    """Start the Authorization Code + PKCE flow."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'oidc_init', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    user_id: int | None = None
    if request.mode == 'link':
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Linking requires authentication',
            )
        if user.telegram_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Telegram is already linked to your account',
            )
        user_id = user.id

    enabled, client_id, _, redirect_uri = await _resolve_oidc_settings(db)
    if not enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Telegram OIDC is not configured')
    if not redirect_uri:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Redirect URI not configured')

    code_verifier, code_challenge = generate_pkce_pair()
    nonce = generate_oidc_nonce()

    extra_data: dict[str, str] = {
        'flow': request.mode,
        'code_verifier': code_verifier,
        'nonce': nonce,
    }
    if request.campaign_slug:
        extra_data['campaign_slug'] = request.campaign_slug
    if request.referral_code:
        extra_data['referral_code'] = request.referral_code
    if user_id is not None:
        extra_data['user_id'] = str(user_id)

    state = await generate_oauth_state('telegram', extra_data=extra_data)

    params = {
        'client_id': client_id,
        'response_type': 'code',
        'scope': 'openid profile',
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'nonce': nonce,
    }
    authorize_url = f'{_OIDC_AUTHORIZE_ENDPOINT}?{urlencode(params)}'

    return TelegramOIDCInitResponse(
        authorize_url=authorize_url,
        state=state,
        expires_in=_OIDC_STATE_TTL_SECONDS,
    )
```

- [ ] **Step 5: Run init tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k oidc_init -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add app/cabinet/dependencies.py app/cabinet/routes/auth.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): POST /auth/telegram/oidc/init Authorization Code + PKCE flow"
```

---

## Task 11: `POST /auth/telegram/oidc/callback` — login branch

**Files:**
- Modify: `app/cabinet/routes/auth.py`
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
@pytest.mark.asyncio
async def test_oidc_callback_invalid_state(app_client, monkeypatch):
    async def _no_state(state, provider):
        return None

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _no_state)

    response = await app_client.post(
        '/auth/telegram/oidc/callback',
        json={'code': 'abc', 'state': 'X' * 64},
    )
    assert response.status_code == 400
    assert 'state' in response.json()['detail'].lower()


@pytest.mark.asyncio
async def test_oidc_callback_login_success(app_client, monkeypatch, make_id_token, jwks_doc):
    from datetime import datetime, UTC

    from app.cabinet.routes import auth as auth_routes
    from app.cabinet.auth import telegram_auth
    from app.cabinet.schemas.auth import AuthResponse, UserResponse

    state_data = {
        'provider': 'telegram',
        'flow': 'login',
        'code_verifier': 'verifier_xyz',
        'nonce': 'nonce_xyz',
    }

    async def _validate_state(state, provider):
        return state_data

    async def _exchange(**kwargs):
        return make_id_token(client_id='111222333', nonce='nonce_xyz')

    async def _fake_get_jwks(force=False):
        return jwks_doc

    async def _no_replay(token_hash, ttl):
        return False

    async def _create_or_login(db, claims, *, campaign_slug, referral_code):
        return AuthResponse(
            access_token='access',
            refresh_token='refresh',
            token_type='bearer',
            expires_in=3600,
            user=UserResponse(
                id=1,
                telegram_id=int(claims.get('id') or claims.get('sub')),
                created_at=datetime.now(UTC),
            ),
        )

    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _validate_state)
    monkeypatch.setattr(auth_routes, 'exchange_authorization_code', _exchange)
    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)
    monkeypatch.setattr(auth_routes.TokenReplayCache, 'is_token_replayed', staticmethod(_no_replay))
    monkeypatch.setattr(auth_routes, '_create_or_login_user_from_oidc_claims', _create_or_login)

    response = await app_client.post(
        '/auth/telegram/oidc/callback',
        json={'code': 'auth_code', 'state': 'S' * 64},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['access_token'] == 'access'
    assert body['user']['telegram_id'] == 1234567890


@pytest.mark.asyncio
async def test_oidc_callback_token_exchange_failure(app_client, monkeypatch):
    state_data = {'provider': 'telegram', 'flow': 'login', 'code_verifier': 'v', 'nonce': 'n'}

    async def _validate_state(state, provider):
        return state_data

    async def _exchange(**kwargs):
        return None

    from app.cabinet.routes import auth as auth_routes
    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _validate_state)
    monkeypatch.setattr(auth_routes, 'exchange_authorization_code', _exchange)

    response = await app_client.post(
        '/auth/telegram/oidc/callback',
        json={'code': 'c', 'state': 'S' * 64},
    )
    assert response.status_code == 502
    assert 'token exchange' in response.json()['detail'].lower()


@pytest.mark.asyncio
async def test_oidc_callback_nonce_mismatch(app_client, monkeypatch, make_id_token, jwks_doc):
    state_data = {'provider': 'telegram', 'flow': 'login', 'code_verifier': 'v', 'nonce': 'expected_nonce'}

    async def _validate_state(state, provider):
        return state_data

    async def _exchange(**kwargs):
        return make_id_token(client_id='111222333', nonce='different_nonce')

    async def _fake_get_jwks(force=False):
        return jwks_doc

    from app.cabinet.routes import auth as auth_routes
    from app.cabinet.auth import telegram_auth
    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _validate_state)
    monkeypatch.setattr(auth_routes, 'exchange_authorization_code', _exchange)
    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)

    response = await app_client.post(
        '/auth/telegram/oidc/callback',
        json={'code': 'c', 'state': 'S' * 64},
    )
    assert response.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k oidc_callback -v`
Expected: 4 failures (404 — endpoint not registered).

- [ ] **Step 3: Implement `oidc_callback`**

Add this handler in `app/cabinet/routes/auth.py` immediately after `oidc_init`:

```python
@router.post('/telegram/oidc/callback')
async def oidc_callback(
    request: TelegramOIDCCallbackRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Authorization Code + PKCE callback. Returns AuthResponse (login) or LinkCallbackResponse (link)."""
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(client_ip, 'oidc_callback', limit=10, window=60, fail_closed=True):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many requests',
            headers={'Retry-After': '60'},
        )

    state_data = await validate_oauth_state(request.state, 'telegram')
    if not state_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid or expired OAuth state',
        )

    flow = state_data.get('flow')
    if flow not in ('login', 'link'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='OAuth state flow mismatch',
        )

    code_verifier = state_data.get('code_verifier')
    nonce = state_data.get('nonce')
    if not code_verifier or not nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='OAuth state is missing required fields',
        )

    enabled, client_id, client_secret, redirect_uri = await _resolve_oidc_settings(db)
    if not enabled or not redirect_uri or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Telegram OIDC is not fully configured',
        )

    id_token = await exchange_authorization_code(
        code=request.code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    if not id_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Token exchange failed (upstream)',
        )

    claims = await validate_telegram_oidc_token(id_token, client_id, expected_nonce=nonce)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram OIDC token',
        )

    token_hash = hashlib.sha256(id_token.encode()).hexdigest()
    token_ttl = max(int(claims.get('exp', 0) - datetime.now(UTC).timestamp()), 60)
    if await TokenReplayCache.is_token_replayed(token_hash, ttl=min(token_ttl, 600)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid or expired Telegram OIDC token',
        )

    if flow == 'login':
        return await _create_or_login_user_from_oidc_claims(
            db,
            claims,
            campaign_slug=state_data.get('campaign_slug'),
            referral_code=state_data.get('referral_code'),
        )

    # flow == 'link' — wired in Task 14
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail='Link flow not yet wired',
    )
```

- [ ] **Step 4: Run callback tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k oidc_callback -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/auth.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): POST /auth/telegram/oidc/callback login branch"
```

---

## Task 12: Deprecate widget endpoints — return HTTP 410

**Files:**
- Modify: `app/cabinet/routes/auth.py` (replace `auth_telegram_widget` and `link_telegram_widget`)
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
@pytest.mark.asyncio
async def test_widget_endpoint_returns_410(app_client):
    response = await app_client.post(
        '/auth/telegram/widget',
        json={
            'id': 123,
            'first_name': 'X',
            'auth_date': 1700000000,
            'hash': 'a' * 64,
        },
    )
    assert response.status_code == 410
    body = response.json()
    assert 'deprecated' in body['detail'].lower()
    assert body['migration_doc'].startswith('https://core.telegram.org/bots/telegram-login')


@pytest.mark.asyncio
async def test_link_widget_endpoint_returns_410(app_client):
    response = await app_client.post(
        '/auth/telegram/link-widget',
        json={
            'id': 123,
            'first_name': 'X',
            'auth_date': 1700000000,
            'hash': 'a' * 64,
        },
    )
    assert response.status_code == 410
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k widget -v`
Expected: FAIL — current endpoint returns 401/422/200, not 410.

- [ ] **Step 3: Replace handlers**

Add `from fastapi.responses import JSONResponse` near the existing `from fastapi import` block. Define the constants near the top of the file:

```python
_WIDGET_DEPRECATION_DETAIL = 'Telegram Login Widget endpoint is deprecated. Use OIDC at /auth/telegram/oidc.'
_WIDGET_DEPRECATION_DOC = 'https://core.telegram.org/bots/telegram-login'
```

Replace the entire `auth_telegram_widget` body (handler is around line 603–707) with:

```python
@router.post('/telegram/widget')
async def auth_telegram_widget(raw_request: Request) -> JSONResponse:
    """Deprecated Telegram Login Widget endpoint. Returns HTTP 410 Gone."""
    logger.warning(
        'Deprecated Telegram widget endpoint called',
        client_ip=get_client_ip(raw_request),
        user_agent=raw_request.headers.get('user-agent'),
    )
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            'detail': _WIDGET_DEPRECATION_DETAIL,
            'migration_doc': _WIDGET_DEPRECATION_DOC,
        },
    )
```

Replace `link_telegram_widget` (around line 884) similarly:

```python
@router.post('/telegram/link-widget')
async def link_telegram_widget(raw_request: Request) -> JSONResponse:
    """Deprecated Telegram Login Widget linking endpoint. Returns HTTP 410 Gone."""
    logger.warning(
        'Deprecated Telegram link-widget endpoint called',
        client_ip=get_client_ip(raw_request),
        user_agent=raw_request.headers.get('user-agent'),
    )
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            'detail': _WIDGET_DEPRECATION_DETAIL,
            'migration_doc': _WIDGET_DEPRECATION_DOC,
        },
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k widget -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/auth.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): deprecate /auth/telegram/widget and /link-widget (HTTP 410)"
```

---

## Task 13: Update `LinkTelegramRequest` and the popup-link branch

**Files:**
- Modify: `app/cabinet/routes/account_linking.py`
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
def test_link_telegram_request_accepts_nonce():
    from app.cabinet.routes.account_linking import LinkTelegramRequest

    req = LinkTelegramRequest(id_token='x.y.z', nonce='nonce_value_123')
    assert req.nonce == 'nonce_value_123'


def test_link_telegram_request_widget_fields_rejected():
    from app.cabinet.routes.account_linking import LinkTelegramRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LinkTelegramRequest(id=1, first_name='X', auth_date=1700000000, hash='a' * 64)


def test_link_telegram_request_nonce_only_with_id_token():
    from app.cabinet.routes.account_linking import LinkTelegramRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LinkTelegramRequest(init_data='abc', nonce='not-allowed-here')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k link_telegram_request -v`
Expected: FAIL — current schema accepts widget fields.

- [ ] **Step 3: Update `LinkTelegramRequest`**

In `app/cabinet/routes/account_linking.py`, replace the `LinkTelegramRequest` class entirely:

```python
class LinkTelegramRequest(BaseModel):
    """Request for linking Telegram via Mini-App initData or OIDC id_token (popup).

    The Authorization Code + PKCE link path goes through `POST /auth/telegram/oidc/init`
    with `mode=link` and `POST /auth/telegram/oidc/callback`.
    """

    model_config = {'extra': 'forbid'}

    init_data: str | None = Field(None, max_length=4096, description='Telegram WebApp initData string')
    id_token: str | None = Field(None, max_length=4096, description='Telegram OIDC id_token (JWT)')
    nonce: str | None = Field(
        None,
        min_length=8,
        max_length=128,
        pattern=r'^[A-Za-z0-9_\-]+$',
        description='Nonce for OIDC popup id_token (must equal claims["nonce"])',
    )

    @model_validator(mode='after')
    def check_exclusive(self) -> 'LinkTelegramRequest':
        has_init = self.init_data is not None
        has_oidc = self.id_token is not None
        modes = sum([has_init, has_oidc])
        if modes > 1:
            raise ValueError('Provide exactly one of: init_data or id_token')
        if modes == 0:
            raise ValueError('Provide one of: init_data or id_token')
        if not has_oidc and self.nonce is not None:
            raise ValueError('nonce is only valid with id_token')
        return self
```

- [ ] **Step 4: Update `link_telegram` handler**

In the same file, drop the `elif request.id is not None and request.hash is not None ...` widget branch (the long block). Update the OIDC branch's `validate_telegram_oidc_token` call to forward the nonce:

```python
claims = await validate_telegram_oidc_token(
    request.id_token,
    oidc_client_id,
    expected_nonce=request.nonce,
)
```

Drop the final `else` that raised on missing widget fields (the schema validator now enforces it).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k link_telegram_request -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add app/cabinet/routes/account_linking.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): drop widget mode from /auth/account/link/telegram, add nonce"
```

---

## Task 14: Wire link branch in `oidc_callback`

**Files:**
- Modify: `app/cabinet/routes/account_linking.py` (extract `_link_telegram_to_user`)
- Modify: `app/cabinet/routes/auth.py` (use the helper for `flow == 'link'`)
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Write failing test**

Append:

```python
@pytest.mark.asyncio
async def test_oidc_callback_link_uses_state_user_id(app_client, monkeypatch, make_id_token, jwks_doc):
    """flow=link callback loads the user from state.user_id and links the telegram_id."""
    state_data = {
        'provider': 'telegram',
        'flow': 'link',
        'user_id': '42',
        'code_verifier': 'v',
        'nonce': 'n',
    }

    async def _validate_state(state, provider):
        return state_data

    async def _exchange(**kwargs):
        return make_id_token(client_id='111222333', nonce='n', telegram_id=999888)

    async def _fake_get_jwks(force=False):
        return jwks_doc

    async def _no_replay(token_hash, ttl):
        return False

    captured: dict = {}

    async def _link_helper(db, user_id, telegram_id, **claims):
        captured['user_id'] = user_id
        captured['telegram_id'] = telegram_id
        from app.cabinet.routes.account_linking import LinkCallbackResponse
        return LinkCallbackResponse(success=True, message='linked')

    from app.cabinet.routes import auth as auth_routes
    from app.cabinet.routes import account_linking
    from app.cabinet.auth import telegram_auth

    monkeypatch.setattr(auth_routes, 'validate_oauth_state', _validate_state)
    monkeypatch.setattr(auth_routes, 'exchange_authorization_code', _exchange)
    monkeypatch.setattr(telegram_auth, '_get_jwks', _fake_get_jwks)
    monkeypatch.setattr(auth_routes.TokenReplayCache, 'is_token_replayed', staticmethod(_no_replay))
    monkeypatch.setattr(account_linking, '_link_telegram_to_user', _link_helper)

    response = await app_client.post(
        '/auth/telegram/oidc/callback',
        json={'code': 'c', 'state': 'S' * 64},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['success'] is True
    assert captured['user_id'] == 42
    assert captured['telegram_id'] == 999888
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py::test_oidc_callback_link_uses_state_user_id -v`
Expected: FAIL — handler still returns 501.

- [ ] **Step 3: Extract `_link_telegram_to_user`**

In `app/cabinet/routes/account_linking.py`, add this helper above `link_telegram`:

```python
async def _link_telegram_to_user(
    db: AsyncSession,
    user_id: int,
    telegram_id: int,
    *,
    telegram_username: str | None = None,
    telegram_first_name: str | None = None,
    telegram_last_name: str | None = None,
) -> LinkCallbackResponse:
    """Link a Telegram account to an existing cabinet user.

    Encapsulates conflict detection, merge-token creation, profile sync, commit, and
    post-link RemnaWave resync.
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='User not found')

    if user.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Telegram is already linked to your account',
        )

    existing_user = await get_user_by_telegram_id(db, telegram_id)
    if existing_user and existing_user.id != user.id:
        logger.info(
            'Telegram linking conflict: telegram_id already linked to another user',
            telegram_id=telegram_id,
            current_user_id=user.id,
            existing_user_id=existing_user.id,
        )
        merge_token = await create_merge_token(
            primary_user_id=user.id,
            secondary_user_id=existing_user.id,
            provider='telegram',
            provider_id=str(telegram_id),
        )
        return LinkCallbackResponse(success=False, merge_required=True, merge_token=merge_token)

    user.telegram_id = telegram_id
    if telegram_username and not user.username:
        user.username = telegram_username
    if telegram_first_name and not user.first_name:
        user.first_name = telegram_first_name
    if telegram_last_name and not user.last_name:
        user.last_name = telegram_last_name
    user.updated_at = datetime.now(UTC)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='This Telegram account was just linked to another user',
        ) from exc

    logger.info('Telegram linked to account', telegram_id=telegram_id, user_id=user.id)

    try:
        from app.services.remnawave_resync_service import resync_user_subscriptions_with_panel

        resync_result = await resync_user_subscriptions_with_panel(db, user)
        logger.info(
            'Post-TG-link resync completed',
            user_id=user.id,
            telegram_id=telegram_id,
            synced=resync_result['synced'],
            failed=resync_result['failed'],
        )
    except Exception as resync_error:
        logger.error('Post-TG-link resync failed (non-fatal)', user_id=user.id, error=resync_error)

    return LinkCallbackResponse(success=True, message='linked')
```

Update the inlined logic in the existing `link_telegram` handler to call `_link_telegram_to_user(db, user.id, telegram_id, ...)` instead of duplicating the conflict + commit + resync code.

- [ ] **Step 4: Wire the link branch in `oidc_callback`**

In `app/cabinet/routes/auth.py`, replace the `flow == 'link'` placeholder (the `raise HTTPException(... 501 ...)`) with:

```python
# flow == 'link'
from app.cabinet.routes.account_linking import _link_telegram_to_user

raw_user_id = state_data.get('user_id')
try:
    state_user_id = int(raw_user_id) if raw_user_id is not None else None
except (TypeError, ValueError):
    state_user_id = None
if state_user_id is None:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='OAuth state missing user_id for link flow',
    )

try:
    telegram_id = int(claims.get('id', claims.get('sub', 0)))
except (ValueError, TypeError) as exc:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail='Invalid user ID in OIDC claims',
    ) from exc

return await _link_telegram_to_user(
    db,
    state_user_id,
    telegram_id,
    telegram_username=claims.get('preferred_username'),
    telegram_first_name=claims.get('name', claims.get('given_name')),
    telegram_last_name=claims.get('family_name'),
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py -k oidc_callback -v`
Expected: 5 passed (4 from Task 11 + new link test).

- [ ] **Step 6: Commit**

```bash
git add app/cabinet/routes/account_linking.py app/cabinet/routes/auth.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): wire link branch in /auth/telegram/oidc/callback"
```

---

## Task 15: Update branding `auth_methods` payload

**Files:**
- Modify: `app/cabinet/routes/branding.py`
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Locate the branding handler**

Run: `grep -n "TELEGRAM_OIDC_ENABLED_KEY\|oidc_enabled" app/cabinet/routes/branding.py`

Identify the response payload (search for the dict that contains `oidc_enabled` or similar near line 913). Note the exact field names — the test must match them.

- [ ] **Step 2: Write failing test**

Append:

```python
@pytest.mark.asyncio
async def test_branding_auth_methods_marks_widget_deprecated(app_client, monkeypatch):
    async def _enabled(db, key):
        return {
            'TELEGRAM_OIDC_ENABLED': 'true',
            'TELEGRAM_OIDC_CLIENT_ID': '111',
            'TELEGRAM_OIDC_REDIRECT_URI': 'https://cab.example.com/cb',
        }.get(key)

    from app.cabinet.routes import branding
    monkeypatch.setattr(branding, 'get_setting_value', _enabled)

    # Replace '/auth/branding/auth-methods' below with the actual route discovered in Step 1.
    response = await app_client.get('/auth/branding/auth-methods')
    assert response.status_code == 200
    body = response.json()
    assert body.get('oidc_code_flow_available') is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py::test_branding_auth_methods_marks_widget_deprecated -v`
Expected: FAIL — `oidc_code_flow_available` field missing.

- [ ] **Step 4: Update `branding.py`**

In the response-building block (around line 913 where `oidc_enabled` is computed), append:

```python
oidc_redirect_uri_val = await get_setting_value(db, 'TELEGRAM_OIDC_REDIRECT_URI')
oidc_redirect_uri = oidc_redirect_uri_val or settings.TELEGRAM_OIDC_REDIRECT_URI
oidc_code_flow_available = bool(oidc_enabled and oidc_redirect_uri)
```

Add `oidc_code_flow_available` and `widget_deprecated=True` to the response dict (use the actual response variable name).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py::test_branding_auth_methods_marks_widget_deprecated -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/cabinet/routes/branding.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): expose oidc_code_flow_available, mark widget deprecated"
```

---

## Task 16: Static test page

**Files:**
- Create: `app/cabinet/static/__init__.py`
- Create: `app/cabinet/static/telegram-login-test.html`
- Modify: `app/webserver/unified_app.py` (add static mount)
- Test: `tests/cabinet/auth/test_telegram_oidc_routes.py`

- [ ] **Step 1: Create empty package marker**

```bash
mkdir -p app/cabinet/static
: > app/cabinet/static/__init__.py
```

- [ ] **Step 2: Create the test page**

Write to `app/cabinet/static/telegram-login-test.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Telegram OIDC test</title>
  <script src="https://telegram.org/js/telegram-widget.js?22" async></script>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
    button { padding: 0.6rem 1rem; font-size: 1rem; margin-right: 0.5rem; }
    pre { background: #f5f5f5; padding: 1rem; border-radius: 6px; overflow: auto; }
    .row { margin: 1rem 0; }
    label { display: block; font-size: 0.85rem; color: #555; }
    input { width: 100%; padding: 0.4rem; font-family: monospace; }
  </style>
</head>
<body>
  <h1>Telegram OIDC test page</h1>
  <p>Manual harness for popup and Authorization Code + PKCE flows. Configure the bot ID below before testing popup.</p>

  <div class="row">
    <label>Bot ID (BotFather → Web Login → Client ID)</label>
    <input id="botId" placeholder="123456789" />
  </div>

  <div class="row">
    <button id="popup-btn">Login (popup)</button>
    <button id="code-btn">Login (code + PKCE)</button>
  </div>

  <h2>Output</h2>
  <pre id="out">(none)</pre>

  <script>
    const out = document.getElementById('out');
    const log = (label, payload) => {
      out.textContent = label + '\n\n' + JSON.stringify(payload, null, 2);
    };

    function genNonce() {
      const a = new Uint8Array(16);
      crypto.getRandomValues(a);
      return Array.from(a, x => x.toString(16).padStart(2, '0')).join('');
    }

    document.getElementById('popup-btn').addEventListener('click', () => {
      const botId = document.getElementById('botId').value.trim();
      if (!botId) {
        out.textContent = 'Enter bot ID first';
        return;
      }
      const nonce = genNonce();
      sessionStorage.setItem('tg_oidc_nonce', nonce);

      Telegram.Login.init({ bot_id: botId, request_access: 'write', nonce }, (idToken) => {
        if (!idToken) {
          out.textContent = 'Popup cancelled or failed';
          return;
        }
        fetch('/auth/telegram/oidc', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id_token: idToken, nonce }),
        })
          .then(r => r.json().then(b => ({ status: r.status, body: b })))
          .then(({ status, body }) => log('POST /auth/telegram/oidc → ' + status, body));
      });
      Telegram.Login.open();
    });

    document.getElementById('code-btn').addEventListener('click', async () => {
      const r = await fetch('/auth/telegram/oidc/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'login' }),
      });
      const body = await r.json();
      log('POST /auth/telegram/oidc/init → ' + r.status, body);
      if (r.ok && body.authorize_url) {
        sessionStorage.setItem('tg_oidc_state', body.state);
        window.location = body.authorize_url;
      }
    });

    const params = new URLSearchParams(location.search);
    const code = params.get('code');
    const state = params.get('state');
    if (code && state) {
      fetch('/auth/telegram/oidc/callback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, state }),
      })
        .then(r => r.json().then(b => ({ status: r.status, body: b })))
        .then(({ status, body }) => log('POST /auth/telegram/oidc/callback → ' + status, body));
    }
  </script>
</body>
</html>
```

- [ ] **Step 3: Mount the static directory**

In `app/webserver/unified_app.py`, find the existing `app.mount('/miniapp/static', ...)` block (around line 111) and add right after it:

```python
cabinet_static_path = Path(__file__).resolve().parent.parent / 'cabinet' / 'static'
if cabinet_static_path.exists():
    try:
        app.mount('/cabinet/static', StaticFiles(directory=cabinet_static_path), name='cabinet-static')
        logger.info('📦 Cabinet static files mounted at /cabinet/static', static_path=cabinet_static_path)
    except Exception as exc:
        logger.error('Failed to mount /cabinet/static', error=str(exc))
```

(`Path` and `StaticFiles` are already imported in this file.)

- [ ] **Step 4: Add a route test**

Append to `tests/cabinet/auth/test_telegram_oidc_routes.py`:

```python
@pytest.mark.asyncio
async def test_static_test_page_mounted(app_client):
    response = await app_client.get('/cabinet/static/telegram-login-test.html')
    assert response.status_code == 200
    assert 'Telegram OIDC test page' in response.text
```

Run: `pytest tests/cabinet/auth/test_telegram_oidc_routes.py::test_static_test_page_mounted -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/static/__init__.py app/cabinet/static/telegram-login-test.html app/webserver/unified_app.py tests/cabinet/auth/test_telegram_oidc_routes.py
git commit -m "feat(cabinet): static test page for Telegram OIDC flows"
```

---

## Task 17: Integration smoke and full-suite run

**Files:** none (verification only).

- [ ] **Step 1: Run the cabinet auth suite**

Run: `pytest tests/cabinet/auth/ -v`
Expected: all tests pass.

- [ ] **Step 2: Run the full repo test suite**

Run: `pytest -q`
Expected: no regressions vs. `main`. Existing widget tests (if any) must now expect 410 — update them in this same task.

- [ ] **Step 3: Manual smoke (optional, requires real bot config)**

If a staging environment with real BotFather credentials is available:
- Set `TELEGRAM_OIDC_ENABLED=true`, `TELEGRAM_OIDC_CLIENT_ID`, `TELEGRAM_OIDC_CLIENT_SECRET`, `TELEGRAM_OIDC_REDIRECT_URI`.
- Open `/cabinet/static/telegram-login-test.html` in a browser.
- Click "Login (popup)" — verify a session cookie / token comes back.
- Click "Login (code + PKCE)" — verify the redirect round-trips and returns tokens.

- [ ] **Step 4: Final commit (only if Step 2 surfaced housekeeping)**

```bash
git add -A
git commit -m "chore(cabinet): green test suite after OIDC rewrite"
```

---

## Self-review notes

- Spec coverage: every section in the spec maps to a task — settings (T2), helpers (T3, T4, T5), exports (T6), schemas (T7), refactor (T8), popup nonce (T9), init (T10), callback login (T11), widget deprecation (T12), link schema + handler (T13, T14), branding (T15), test page (T16), final verify (T17).
- No placeholders. Each step contains actual code or commands.
- Type consistency: helper names (`_create_or_login_user_from_oidc_claims`, `_link_telegram_to_user`, `_resolve_oidc_settings`, `_optional_cabinet_user`) are referenced consistently across tasks. PKCE pair `(verifier, challenge)` matches across tests and impl. The `expected_nonce` keyword arg is consistent between `validate_telegram_oidc_token` and all callers.
