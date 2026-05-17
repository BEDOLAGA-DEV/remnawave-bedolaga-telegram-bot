"""OAuth 2.0 provider implementations for cabinet authentication."""

import asyncio
import base64
import hashlib
import json
import secrets
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

import httpx
import jwt as pyjwt
import structlog
from pydantic import BaseModel

from app.config import settings
from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

STATE_TTL_SECONDS = 600  # 10 minutes
GOOGLE_ISSUERS = {'accounts.google.com', 'https://accounts.google.com'}
GOOGLE_JWKS_URL = 'https://www.googleapis.com/oauth2/v3/certs'
APPLE_ISSUER = 'https://appleid.apple.com'
APPLE_JWKS_URL = 'https://appleid.apple.com/auth/keys'
APPLE_CLIENT_SECRET_TTL = timedelta(days=180)
GoogleClientType = Literal['web', 'ios', 'android']
AppleClientType = Literal['web', 'ios']

_google_jwks_cache: dict[str, Any] = {}
_google_jwks_cache_expiry: datetime | None = None
_GOOGLE_JWKS_CACHE_TTL_SECONDS = 3600
_google_jwks_lock = asyncio.Lock()
_google_jwks_last_force_refresh: datetime | None = None
_GOOGLE_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS = 30

_apple_jwks_cache: dict[str, Any] = {}
_apple_jwks_cache_expiry: datetime | None = None
_APPLE_JWKS_CACHE_TTL_SECONDS = 3600
_apple_jwks_lock = asyncio.Lock()
_apple_jwks_last_force_refresh: datetime | None = None
_APPLE_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS = 30

_JWK_KTY_TO_ALGORITHM_CLASS: dict[str, Any] = {
    'RSA': pyjwt.algorithms.RSAAlgorithm,
    'EC': pyjwt.algorithms.ECAlgorithm,
    'OKP': pyjwt.algorithms.OKPAlgorithm,
}
_JWK_KTY_DEFAULT_ALG: dict[str, str] = {
    'RSA': 'RS256',
    'EC': 'ES256',
    'OKP': 'EdDSA',
}


# --- Typed dicts for provider API responses ---


class OAuthProviderConfig(TypedDict):
    client_id: str
    client_secret: str
    enabled: bool
    display_name: str
    team_id: str
    key_id: str
    private_key: str
    web_client_id: str
    ios_client_id: str
    android_client_id: str


class OAuthTokenResponse(TypedDict, total=False):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    scope: str
    # Provider-specific extra fields (optional)
    email: str
    user_id: int
    id_token: str
    _google_client_type: GoogleClientType
    _google_nonce: str
    _apple_nonce: str
    _apple_user: Any
    _apple_client_type: AppleClientType


class GoogleUserInfoResponse(TypedDict, total=False):
    sub: str
    email: str
    email_verified: bool
    given_name: str
    family_name: str
    picture: str
    name: str


class YandexUserInfoResponse(TypedDict, total=False):
    id: str
    login: str
    default_email: str
    emails: list[str]
    first_name: str
    last_name: str
    default_avatar_id: str


class DiscordUserInfoResponse(TypedDict, total=False):
    id: str
    username: str
    global_name: str
    email: str
    verified: bool
    avatar: str


class VKIDUserData(TypedDict, total=False):
    """VK ID /oauth2/user_info response user object."""

    user_id: str
    first_name: str
    last_name: str
    phone: str
    avatar: str
    email: str


class VKIDUserInfoResponse(TypedDict, total=False):
    user: VKIDUserData


class AppleUserName(TypedDict, total=False):
    firstName: str
    lastName: str


class AppleUserPayload(TypedDict, total=False):
    name: AppleUserName
    email: str


# --- Models ---


class OAuthUserInfo(BaseModel):
    """Normalized user info from OAuth provider."""

    provider: str
    provider_id: str
    email: str | None = None
    email_verified: bool = False
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    avatar_url: str | None = None


# --- CSRF state management (Redis) ---


async def generate_oauth_state(provider: str, extra_data: dict[str, str] | None = None) -> str:
    """Generate a CSRF state token for OAuth flow.

    Stores provider name and optional extra data (e.g., PKCE code_verifier) in Redis with TTL.
    Keys prefixed with '_' are ephemeral and NOT stored in Redis (e.g., _code_challenge).
    CacheService handles JSON serialization internally.
    """
    state = secrets.token_urlsafe(32)
    value: dict[str, Any] = {'provider': provider}
    if extra_data:
        # Filter out ephemeral keys (prefixed with '_') — they're only needed for the URL
        value.update({k: v for k, v in extra_data.items() if not k.startswith('_')})
    stored = await cache.set(cache_key('oauth_state', state), value, expire=STATE_TTL_SECONDS)
    if not stored:
        logger.error('Failed to store OAuth state in Redis')
        raise RuntimeError('Failed to store OAuth state')
    return state


async def validate_oauth_state(state: str, provider: str | None = None) -> dict[str, Any] | None:
    """Validate and consume a CSRF state token from Redis.

    Uses atomic GETDEL to prevent TOCTOU race conditions.
    Returns the stored data dict (with 'provider' key + any extra data) or None if invalid.

    Args:
        state: The state token to validate.
        provider: If provided, verifies it matches the stored provider.
                  If None, skips provider check (used for server-complete flow).
    """
    key = cache_key('oauth_state', state)
    data: Any = await cache.getdel(key)
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    if provider is not None and data.get('provider') != provider:
        return None
    return data


# --- Provider implementations ---


class OAuthProvider(ABC):
    """Base class for OAuth 2.0 providers."""

    name: str
    display_name: str

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def prepare_auth_state(self) -> dict[str, str]:
        """Return extra data to store with OAuth state (e.g., PKCE code_verifier).

        Override in providers that need PKCE or other state-stored data.
        The returned dict is stored in Redis alongside the state token
        and passed back via validate_oauth_state().
        """
        return {}

    @abstractmethod
    def get_authorization_url(self, state: str, **kwargs: Any) -> str:
        """Build the authorization URL for the provider.

        kwargs may contain extra data from prepare_auth_state() (e.g., code_challenge).
        """

    @abstractmethod
    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
        """Exchange authorization code for tokens.

        kwargs may contain provider-specific params (e.g., device_id, code_verifier for VK).
        """

    @abstractmethod
    async def get_user_info(self, token_data: OAuthTokenResponse) -> OAuthUserInfo:
        """Fetch user info from the provider."""


class GoogleProvider(OAuthProvider):
    name = 'google'
    display_name = 'Google'

    AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
    TOKEN_URL = 'https://oauth2.googleapis.com/token'
    USERINFO_URL = 'https://www.googleapis.com/oauth2/v3/userinfo'

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        ios_client_id: str = '',
        android_client_id: str = '',
    ) -> None:
        super().__init__(client_id, client_secret, redirect_uri)
        self.web_client_id = client_id
        self.ios_client_id = ios_client_id
        self.android_client_id = android_client_id

    def get_authorization_url(self, state: str, **kwargs: Any) -> str:
        params: dict[str, str] = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': 'openid email profile',
            'state': state,
            'access_type': 'offline',
            'prompt': 'select_account',
        }
        request = httpx.Request('GET', self.AUTHORIZE_URL, params=params)
        return str(request.url)

    def _client_id_for(self, client_type: str | None) -> str:
        if client_type == 'ios':
            if not self.ios_client_id:
                raise ValueError('Google iOS client ID is not configured')
            return self.ios_client_id
        if client_type == 'android':
            if not self.android_client_id:
                raise ValueError('Google Android client ID is not configured')
            return self.android_client_id
        if client_type == 'web':
            if not self.web_client_id:
                raise ValueError('Google web client ID is not configured')
            return self.web_client_id
        raise ValueError('Unsupported Google client type')

    def ensure_client_type_configured(self, client_type: str | None) -> None:
        self._client_id_for(client_type)

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
        id_token = kwargs.get('id_token')
        if id_token:
            client_type: GoogleClientType = kwargs.get('client_type', 'web')
            self._client_id_for(client_type)
            data: OAuthTokenResponse = {
                'id_token': id_token,
                '_google_client_type': client_type,
            }
            if kwargs.get('nonce'):
                data['_google_nonce'] = kwargs['nonce']
            return data
        if not code:
            raise ValueError('Authorization code is required for Google web OAuth')

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                json={
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'code': code,
                    'grant_type': 'authorization_code',
                    'redirect_uri': self.redirect_uri,
                },
            )
            response.raise_for_status()
            data: OAuthTokenResponse = response.json()
            return data

    async def get_user_info(self, token_data: OAuthTokenResponse) -> OAuthUserInfo:
        access_token = token_data.get('access_token')
        if not access_token:
            id_token = token_data.get('id_token')
            if not id_token:
                raise ValueError('Google token response missing access_token or id_token')

            client_id = self._client_id_for(token_data.get('_google_client_type', 'web'))
            claims = await validate_google_id_token(id_token, client_id, token_data.get('_google_nonce'))
            if not claims:
                raise ValueError('Google id_token validation failed')

            provider_id = claims.get('sub')
            if not provider_id:
                raise ValueError('Google id_token missing sub')

            email_claim = claims.get('email')
            email = email_claim.strip() if isinstance(email_claim, str) and email_claim.strip() else None
            return OAuthUserInfo(
                provider='google',
                provider_id=str(provider_id),
                email=email,
                email_verified=bool(email and _truthy_claim(claims.get('email_verified'))),
                first_name=claims.get('given_name'),
                last_name=claims.get('family_name'),
                avatar_url=claims.get('picture'),
            )

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self.USERINFO_URL,
                headers={'Authorization': f'Bearer {access_token}'},
            )
            response.raise_for_status()
            data: GoogleUserInfoResponse = response.json()

        return OAuthUserInfo(
            provider='google',
            provider_id=str(data['sub']),
            email=data.get('email'),
            email_verified=data.get('email_verified', False),
            first_name=data.get('given_name'),
            last_name=data.get('family_name'),
            avatar_url=data.get('picture'),
        )


class YandexProvider(OAuthProvider):
    name = 'yandex'
    display_name = 'Yandex'

    AUTHORIZE_URL = 'https://oauth.yandex.com/authorize'
    TOKEN_URL = 'https://oauth.yandex.com/token'
    USERINFO_URL = 'https://login.yandex.ru/info'

    def get_authorization_url(self, state: str, **kwargs: Any) -> str:
        params: dict[str, str] = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': 'login:info login:email',
            'state': state,
            'force_confirm': 'yes',
        }
        request = httpx.Request('GET', self.AUTHORIZE_URL, params=params)
        return str(request.url)

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'code': code,
                    'grant_type': 'authorization_code',
                },
            )
            response.raise_for_status()
            data: OAuthTokenResponse = response.json()
            return data

    async def get_user_info(self, token_data: OAuthTokenResponse) -> OAuthUserInfo:
        access_token = token_data['access_token']
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self.USERINFO_URL,
                params={'format': 'json'},
                headers={'Authorization': f'OAuth {access_token}'},
            )
            response.raise_for_status()
            data: YandexUserInfoResponse = response.json()

        default_email = data.get('default_email')
        emails = data.get('emails', [])
        email = default_email or (emails[0] if emails else None)

        return OAuthUserInfo(
            provider='yandex',
            provider_id=str(data['id']),
            email=email,
            # Yandex не возвращает proof-of-ownership flag, но default email обычно
            # привязан и юзается провайдером. Помечаем как verified для UX (recovery,
            # account linking, panel sync), а защита от admin escalation работает
            # через email_verification_source='oauth_yandex' — этот источник НЕ в
            # TRUSTED_EMAIL_VERIFICATION_SOURCES, поэтому match с ADMIN_EMAILS
            # для Superadmin grant не сработает.
            email_verified=bool(email),
            first_name=data.get('first_name'),
            last_name=data.get('last_name'),
            username=data.get('login'),
            avatar_url=(
                f'https://avatars.yandex.net/get-yapic/{data["default_avatar_id"]}/islands-200'
                if data.get('default_avatar_id')
                else None
            ),
        )


class DiscordProvider(OAuthProvider):
    name = 'discord'
    display_name = 'Discord'

    AUTHORIZE_URL = 'https://discord.com/api/oauth2/authorize'
    TOKEN_URL = 'https://discord.com/api/oauth2/token'
    USERINFO_URL = 'https://discord.com/api/v10/users/@me'

    def get_authorization_url(self, state: str, **kwargs: Any) -> str:
        params: dict[str, str] = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': 'identify email',
            'state': state,
            'prompt': 'consent',
        }
        request = httpx.Request('GET', self.AUTHORIZE_URL, params=params)
        return str(request.url)

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'code': code,
                    'grant_type': 'authorization_code',
                    'redirect_uri': self.redirect_uri,
                },
            )
            response.raise_for_status()
            data: OAuthTokenResponse = response.json()
            return data

    async def get_user_info(self, token_data: OAuthTokenResponse) -> OAuthUserInfo:
        access_token = token_data['access_token']
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self.USERINFO_URL,
                headers={'Authorization': f'Bearer {access_token}'},
            )
            response.raise_for_status()
            data: DiscordUserInfoResponse = response.json()

        avatar_url: str | None = None
        if data.get('avatar'):
            avatar_url = f'https://cdn.discordapp.com/avatars/{data["id"]}/{data["avatar"]}.png'

        return OAuthUserInfo(
            provider='discord',
            provider_id=str(data['id']),
            email=data.get('email'),
            email_verified=data.get('verified', False),
            first_name=data.get('global_name') or data.get('username'),
            username=data.get('username'),
            avatar_url=avatar_url,
        )


class VKProvider(OAuthProvider):
    """VK ID OAuth 2.1 provider (id.vk.ru).

    Uses OAuth 2.1 with mandatory PKCE (S256).
    Old oauth.vk.com endpoints deprecated since September 30, 2025.
    """

    name = 'vk'
    display_name = 'VK'

    AUTHORIZE_URL = 'https://id.vk.ru/authorize'
    TOKEN_URL = 'https://id.vk.ru/oauth2/auth'
    USERINFO_URL = 'https://id.vk.ru/oauth2/user_info'

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """Generate PKCE code_verifier and code_challenge (S256)."""
        code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
        return code_verifier, code_challenge

    def prepare_auth_state(self) -> dict[str, str]:
        """Generate PKCE pair. code_verifier stored in Redis, code_challenge only goes to URL."""
        code_verifier, code_challenge = self._generate_pkce()
        # code_challenge is ephemeral — only needed for the authorization URL,
        # not stored in Redis (code_verifier is the secret used during token exchange)
        return {
            'code_verifier': code_verifier,
            '_code_challenge': code_challenge,
        }

    def get_authorization_url(self, state: str, **kwargs: Any) -> str:
        code_challenge: str = kwargs.get('_code_challenge', '')
        params: dict[str, str] = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': 'vkid.personal_info email',
            'state': state,
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
        }
        request = httpx.Request('GET', self.AUTHORIZE_URL, params=params)
        return str(request.url)

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
        device_id: str = kwargs.get('device_id', '')
        code_verifier: str = kwargs.get('code_verifier', '')
        state: str = kwargs.get('state', '')

        if not device_id:
            raise ValueError('device_id is required for VK ID token exchange')
        if not code_verifier:
            raise ValueError('code_verifier is required for VK ID token exchange')

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': self.redirect_uri,
                    'client_id': self.client_id,
                    'device_id': device_id,
                    'code_verifier': code_verifier,
                    'state': state,
                },
            )
            response.raise_for_status()
            data: OAuthTokenResponse = response.json()
            return data

    async def get_user_info(self, token_data: OAuthTokenResponse) -> OAuthUserInfo:
        access_token = token_data['access_token']

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.USERINFO_URL,
                data={
                    'access_token': access_token,
                    'client_id': self.client_id,
                },
            )
            response.raise_for_status()
            data: VKIDUserInfoResponse = response.json()

        user_data = data.get('user')
        if not user_data:
            raise ValueError('VK ID response missing user data')

        user_id = user_data.get('user_id')
        if not user_id:
            raise ValueError('VK ID response missing user_id')

        # VK ID returns email only if 'email' scope was granted and user has a verified email
        email: str | None = user_data.get('email') or None

        return OAuthUserInfo(
            provider='vk',
            provider_id=str(user_id),
            email=email,
            # VK ID не cryptographically proves email ownership, но если юзер
            # прошёл OAuth flow и VK выдал email — обычно он валидный. Помечаем
            # как verified для UX; защита от admin-escalation выполняется на
            # уровне email_verification_source='oauth_vk' (не trusted для
            # ADMIN_EMAILS match — см. TRUSTED_EMAIL_VERIFICATION_SOURCES).
            email_verified=bool(email),
            first_name=user_data.get('first_name'),
            last_name=user_data.get('last_name'),
            avatar_url=user_data.get('avatar'),
        )


def _build_jwks_public_keys(jwks_data: dict[str, Any], *, log_context: str) -> dict[str, tuple[Any, str]]:
    """Build {kid: (public_key, alg)} mapping from a JWKS document."""
    public_keys: dict[str, tuple[Any, str]] = {}
    for key_data in jwks_data.get('keys', []):
        kid = key_data.get('kid')
        if not kid:
            continue
        kty = key_data.get('kty')
        algorithm_cls = _JWK_KTY_TO_ALGORITHM_CLASS.get(kty)
        if algorithm_cls is None:
            logger.debug('OAuth JWKS: skipping JWK with unsupported kty', provider=log_context, kid=kid, kty=kty)
            continue
        try:
            public_key = algorithm_cls.from_jwk(key_data)
        except Exception as exc:
            logger.warning('OAuth JWKS: failed to load JWK', provider=log_context, kid=kid, kty=kty, error=str(exc)[:200])
            continue
        alg = key_data.get('alg') or _JWK_KTY_DEFAULT_ALG.get(kty, '')
        public_keys[kid] = (public_key, alg)
    return public_keys


async def _get_google_jwks(force: bool = False) -> dict[str, Any]:
    """Fetch and cache Google's JWKS for Google Sign-In id_token verification."""
    global _google_jwks_cache, _google_jwks_cache_expiry

    now = datetime.now(UTC)
    if not force and _google_jwks_cache and _google_jwks_cache_expiry and now < _google_jwks_cache_expiry:
        return _google_jwks_cache

    async with _google_jwks_lock:
        now = datetime.now(UTC)
        if not force and _google_jwks_cache and _google_jwks_cache_expiry and now < _google_jwks_cache_expiry:
            return _google_jwks_cache

        proxy = settings.PROXY_URL if hasattr(settings, 'PROXY_URL') and settings.PROXY_URL else None
        async with httpx.AsyncClient(timeout=10, proxy=proxy) as client:
            response = await client.get(GOOGLE_JWKS_URL)
            response.raise_for_status()
            _google_jwks_cache = response.json()
            _google_jwks_cache_expiry = now + timedelta(seconds=_GOOGLE_JWKS_CACHE_TTL_SECONDS)
            return _google_jwks_cache


async def _force_refresh_google_jwks(kid: str) -> dict[str, Any] | None:
    """Refresh Google's JWKS with cooldown protection for key rotation."""
    global _google_jwks_cache_expiry, _google_jwks_last_force_refresh

    async with _google_jwks_lock:
        now = datetime.now(UTC)
        if (
            _google_jwks_last_force_refresh
            and (now - _google_jwks_last_force_refresh).total_seconds() < _GOOGLE_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS
        ):
            logger.warning('Google OAuth: JWKS force refresh on cooldown', kid=kid)
            return None
        _google_jwks_last_force_refresh = now
        _google_jwks_cache_expiry = None

    return await _get_google_jwks(force=True)


async def _get_apple_jwks(force: bool = False) -> dict[str, Any]:
    """Fetch and cache Apple's JWKS for Sign in with Apple id_token verification."""
    global _apple_jwks_cache, _apple_jwks_cache_expiry

    now = datetime.now(UTC)
    if not force and _apple_jwks_cache and _apple_jwks_cache_expiry and now < _apple_jwks_cache_expiry:
        return _apple_jwks_cache

    async with _apple_jwks_lock:
        now = datetime.now(UTC)
        if not force and _apple_jwks_cache and _apple_jwks_cache_expiry and now < _apple_jwks_cache_expiry:
            return _apple_jwks_cache

        proxy = settings.PROXY_URL if hasattr(settings, 'PROXY_URL') and settings.PROXY_URL else None
        async with httpx.AsyncClient(timeout=10, proxy=proxy) as client:
            response = await client.get(APPLE_JWKS_URL)
            response.raise_for_status()
            _apple_jwks_cache = response.json()
            _apple_jwks_cache_expiry = now + timedelta(seconds=_APPLE_JWKS_CACHE_TTL_SECONDS)
            return _apple_jwks_cache


async def _force_refresh_apple_jwks(kid: str) -> dict[str, Any] | None:
    """Refresh Apple's JWKS with cooldown protection for key rotation."""
    global _apple_jwks_cache_expiry, _apple_jwks_last_force_refresh

    async with _apple_jwks_lock:
        now = datetime.now(UTC)
        if (
            _apple_jwks_last_force_refresh
            and (now - _apple_jwks_last_force_refresh).total_seconds() < _APPLE_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS
        ):
            logger.warning('Apple OAuth: JWKS force refresh on cooldown', kid=kid)
            return None
        _apple_jwks_last_force_refresh = now
        _apple_jwks_cache_expiry = None

    return await _get_apple_jwks(force=True)


def _sha256_urlsafe(value: str) -> str:
    digest = hashlib.sha256(value.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _truthy_claim(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {'true', '1'}
    return bool(value)


def _parse_apple_user_payload(value: Any) -> AppleUserPayload:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
    elif isinstance(value, dict):
        parsed = value
    else:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


async def validate_google_id_token(id_token: str, client_id: str, nonce: str | None = None) -> dict[str, Any] | None:
    """Validate a Google Sign-In id_token for the expected OAuth client ID and nonce."""
    try:
        jwks_data = await _get_google_jwks()
        public_keys = _build_jwks_public_keys(jwks_data, log_context='Google OAuth')

        unverified_header = pyjwt.get_unverified_header(id_token)
        kid = unverified_header.get('kid')
        if kid and kid not in public_keys:
            refreshed = await _force_refresh_google_jwks(kid)
            if refreshed:
                public_keys = _build_jwks_public_keys(refreshed, log_context='Google OAuth')

        if not kid or kid not in public_keys:
            logger.warning('Google OAuth: unknown kid in id_token', kid=kid)
            return None

        public_key, key_alg = public_keys[kid]
        claims = pyjwt.decode(
            id_token,
            key=public_key,
            algorithms=[key_alg],
            audience=client_id,
            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']},
        )

        if claims.get('iss') not in GOOGLE_ISSUERS:
            logger.warning('Google OAuth: invalid issuer in id_token', issuer=claims.get('iss'))
            return None

        if nonce:
            token_nonce = claims.get('nonce')
            if token_nonce not in {nonce, _sha256_urlsafe(nonce)}:
                logger.warning('Google OAuth: nonce mismatch in id_token')
                return None

        return claims
    except pyjwt.ExpiredSignatureError:
        logger.warning('Google OAuth: id_token expired')
        return None
    except pyjwt.InvalidTokenError as exc:
        logger.warning('Google OAuth: invalid id_token', error=str(exc))
        return None
    except httpx.HTTPError as exc:
        logger.error('Google OAuth: failed to fetch JWKS', error=str(exc))
        return None


async def validate_apple_id_token(id_token: str, client_id: str, nonce: str | None = None) -> dict[str, Any] | None:
    """Validate a Sign in with Apple id_token and optional nonce."""
    try:
        jwks_data = await _get_apple_jwks()
        public_keys = _build_jwks_public_keys(jwks_data, log_context='Apple OAuth')

        unverified_header = pyjwt.get_unverified_header(id_token)
        kid = unverified_header.get('kid')
        if kid and kid not in public_keys:
            refreshed = await _force_refresh_apple_jwks(kid)
            if refreshed:
                public_keys = _build_jwks_public_keys(refreshed, log_context='Apple OAuth')

        if not kid or kid not in public_keys:
            logger.warning('Apple OAuth: unknown kid in id_token', kid=kid)
            return None

        public_key, key_alg = public_keys[kid]
        claims = pyjwt.decode(
            id_token,
            key=public_key,
            algorithms=[key_alg],
            audience=client_id,
            issuer=APPLE_ISSUER,
            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']},
        )

        if nonce:
            token_nonce = claims.get('nonce')
            # Web flows use the raw nonce; native iOS best practice sends SHA-256(raw_nonce).
            if token_nonce not in {nonce, _sha256_urlsafe(nonce)}:
                logger.warning('Apple OAuth: nonce mismatch in id_token')
                return None

        return claims
    except pyjwt.ExpiredSignatureError:
        logger.warning('Apple OAuth: id_token expired')
        return None
    except pyjwt.InvalidTokenError as exc:
        logger.warning('Apple OAuth: invalid id_token', error=str(exc))
        return None
    except httpx.HTTPError as exc:
        logger.error('Apple OAuth: failed to fetch JWKS', error=str(exc))
        return None


class AppleProvider(OAuthProvider):
    """Sign in with Apple provider using Apple's REST/OIDC endpoints."""

    name = 'apple'
    display_name = 'Apple'

    AUTHORIZE_URL = 'https://appleid.apple.com/auth/authorize'
    TOKEN_URL = 'https://appleid.apple.com/auth/token'

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        web_client_id: str,
        ios_client_id: str,
        team_id: str,
        key_id: str,
        private_key: str,
    ) -> None:
        super().__init__(client_id, client_secret, redirect_uri)
        self.web_client_id = web_client_id
        self.ios_client_id = ios_client_id
        self.team_id = team_id
        self.key_id = key_id
        self.private_key = private_key

    def prepare_auth_state(self) -> dict[str, str]:
        nonce = secrets.token_urlsafe(32)
        return {
            'nonce': nonce,
            '_nonce': nonce,
        }

    def get_authorization_url(self, state: str, **kwargs: Any) -> str:
        nonce: str = kwargs.get('_nonce', '')
        client_type: AppleClientType = kwargs.get('_client_type', 'web')
        params: dict[str, str] = {
            'client_id': self._client_id_for(client_type),
            'redirect_uri': self.redirect_uri,
            'response_type': 'code id_token',
            'scope': 'name email',
            'response_mode': 'form_post',
            'state': state,
            'nonce': nonce,
        }
        request = httpx.Request('GET', self.AUTHORIZE_URL, params=params)
        return str(request.url)

    def _client_id_for(self, client_type: str | None) -> str:
        if client_type == 'ios':
            if not self.ios_client_id:
                raise ValueError('Apple iOS client ID is not configured')
            return self.ios_client_id
        if client_type == 'web':
            if not self.web_client_id:
                raise ValueError('Apple web client ID is not configured')
            return self.web_client_id
        raise ValueError('Unsupported Apple client type')

    def create_client_secret(self, client_type: AppleClientType = 'web', now: datetime | None = None) -> str:
        client_id = self._client_id_for(client_type)
        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + APPLE_CLIENT_SECRET_TTL
        return pyjwt.encode(
            {
                'iss': self.team_id,
                'iat': int(issued_at.timestamp()),
                'exp': int(expires_at.timestamp()),
                'aud': APPLE_ISSUER,
                'sub': client_id,
            },
            self.private_key,
            algorithm='ES256',
            headers={'kid': self.key_id},
        )

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
        if not self.team_id or not self.key_id or not self.private_key:
            raise ValueError('Apple Sign in is missing team_id, key_id, or private_key')

        client_type: AppleClientType = kwargs.get('client_type', 'web')
        client_id = self._client_id_for(client_type)
        token_request_data = {
            'client_id': client_id,
            'client_secret': self.create_client_secret(client_type=client_type),
            'code': code,
            'grant_type': 'authorization_code',
        }
        if client_type == 'web':
            token_request_data['redirect_uri'] = self.redirect_uri

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                data=token_request_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
            )
            response.raise_for_status()
            data: OAuthTokenResponse = response.json()

        if not data.get('id_token'):
            raise ValueError('Apple token response missing id_token')
        if kwargs.get('nonce'):
            data['_apple_nonce'] = kwargs['nonce']
        if kwargs.get('user') is not None:
            data['_apple_user'] = kwargs['user']
        data['_apple_client_type'] = client_type
        return data

    async def get_user_info(self, token_data: OAuthTokenResponse) -> OAuthUserInfo:
        id_token = token_data.get('id_token')
        if not id_token:
            raise ValueError('Apple token response missing id_token')

        client_id = self._client_id_for(token_data.get('_apple_client_type', 'web'))
        claims = await validate_apple_id_token(id_token, client_id, token_data.get('_apple_nonce'))
        if not claims:
            raise ValueError('Apple id_token validation failed')

        provider_id = claims.get('sub')
        if not provider_id:
            raise ValueError('Apple id_token missing sub')

        apple_user = _parse_apple_user_payload(token_data.get('_apple_user'))
        name = apple_user.get('name') or {}
        email_claim = claims.get('email')
        email = email_claim.strip() if isinstance(email_claim, str) and email_claim.strip() else None

        return OAuthUserInfo(
            provider='apple',
            provider_id=str(provider_id),
            email=email,
            email_verified=bool(email and _truthy_claim(claims.get('email_verified'))),
            first_name=name.get('firstName') if isinstance(name, dict) else None,
            last_name=name.get('lastName') if isinstance(name, dict) else None,
        )


# --- Provider factory ---

_PROVIDERS: dict[str, type[OAuthProvider]] = {
    'google': GoogleProvider,
    'yandex': YandexProvider,
    'discord': DiscordProvider,
    'vk': VKProvider,
    'apple': AppleProvider,
}


def get_provider(name: str) -> OAuthProvider | None:
    """Get an OAuth provider instance if enabled.

    Returns None if the provider is not enabled or not found.
    """
    providers_config: dict[str, OAuthProviderConfig] = settings.get_oauth_providers_config()
    config = providers_config.get(name)
    if not config or not config['enabled']:
        return None

    provider_class = _PROVIDERS.get(name)
    if not provider_class:
        return None

    redirect_uri = f'{settings.CABINET_URL}/auth/oauth/callback'

    if provider_class is AppleProvider:
        return provider_class(
            client_id=config['client_id'],
            client_secret=config['client_secret'],
            redirect_uri=redirect_uri,
            web_client_id=config['web_client_id'],
            ios_client_id=config['ios_client_id'],
            team_id=config['team_id'],
            key_id=config['key_id'],
            private_key=config['private_key'],
        )

    if provider_class is GoogleProvider:
        return provider_class(
            client_id=config['client_id'],
            client_secret=config['client_secret'],
            redirect_uri=redirect_uri,
            ios_client_id=config['ios_client_id'],
            android_client_id=config['android_client_id'],
        )

    return provider_class(
        client_id=config['client_id'],
        client_secret=config['client_secret'],
        redirect_uri=redirect_uri,
    )
