"""OAuth 2.0 provider implementations for cabinet authentication."""

import base64
import hashlib
import secrets
from abc import ABC, abstractmethod
from typing import Any, TypedDict

import httpx
import structlog
from pydantic import BaseModel

from app.config import settings
from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

STATE_TTL_SECONDS = 600  # 10 minutes


# --- Typed dicts for provider API responses ---


class OAuthProviderConfig(TypedDict):
    client_id: str
    client_secret: str
    enabled: bool
    display_name: str


class OAuthTokenResponse(TypedDict, total=False):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    scope: str
    # Provider-specific extra fields (optional)
    email: str
    user_id: int


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

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
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
        access_token = token_data['access_token']
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
            email_verified=bool(email),
            first_name=user_data.get('first_name'),
            last_name=user_data.get('last_name'),
            avatar_url=user_data.get('avatar'),
        )



class TelegramOIDCProvider(OAuthProvider):
    """Telegram OpenID Connect provider (oauth.telegram.org).

    Uses OAuth 2.0 + OIDC with PKCE (S256).
    Returns user info via id_token JWT (no userinfo endpoint needed).
    """

    name = 'telegram'
    display_name = 'Telegram'

    AUTHORIZE_URL = 'https://oauth.telegram.org/auth'
    TOKEN_URL = 'https://oauth.telegram.org/token'

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """Generate PKCE code_verifier and code_challenge (S256)."""
        code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
        return code_verifier, code_challenge

    def prepare_auth_state(self) -> dict[str, str]:
        code_verifier, code_challenge = self._generate_pkce()
        return {
            'code_verifier': code_verifier,
            '_code_challenge': code_challenge,
        }

    def get_authorization_url(self, state: str, **kwargs: Any) -> str:
        code_challenge: str = kwargs.get('_code_challenge', '')
        # Extract origin from redirect_uri (scheme + host)
        from urllib.parse import urlparse, quote
        parsed = urlparse(self.redirect_uri)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        # Build URL manually to use %20 encoding (not +) for scope,
        # matching Telegram's expected format to avoid extra redirects
        encoded_redirect = quote(self.redirect_uri, safe='')
        encoded_origin = quote(origin, safe='')
        return (
            f'{self.AUTHORIZE_URL}'
            f'?client_id={self.client_id}'
            f'&origin={encoded_origin}'
            f'&return_to={encoded_redirect}'
            f'&scope=openid%20profile'
            f'&state={state}'
            f'&redirect_uri={encoded_redirect}'
            f'&code_challenge={code_challenge}'
            f'&code_challenge_method=S256'
            f'&response_type=code'
        )

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthTokenResponse:
        code_verifier: str = kwargs.get('code_verifier', '')
        if not code_verifier:
            raise ValueError('code_verifier is required for Telegram OIDC token exchange')

        # Telegram requires Basic Authorization for token exchange
        credentials = base64.b64encode(f'{self.client_id}:{self.client_secret}'.encode()).decode()

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_URL,
                headers={'Authorization': f'Basic {credentials}'},
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'redirect_uri': self.redirect_uri,
                    'code_verifier': code_verifier,
                },
            )
            response.raise_for_status()
            data: OAuthTokenResponse = response.json()
            return data

    async def get_user_info(self, token_data: OAuthTokenResponse) -> OAuthUserInfo:
        import jwt as pyjwt

        id_token = token_data.get('id_token', '')
        if not id_token:
            raise ValueError('Telegram OIDC response missing id_token')

        # Decode without verification — token comes directly from Telegram's token endpoint
        claims = pyjwt.decode(id_token, options={'verify_signature': False})

        # Telegram OIDC claims: sub, id, name, preferred_username, picture, phone_number
        telegram_id = str(claims.get('id') or claims.get('sub'))
        if not telegram_id:
            raise ValueError('Telegram OIDC id_token missing user id')

        name = claims.get('name', '')
        first_name = name.split(' ', 1)[0] if name else None
        last_name = name.split(' ', 1)[1] if name and ' ' in name else None

        return OAuthUserInfo(
            provider='telegram',
            provider_id=telegram_id,
            first_name=first_name,
            last_name=last_name,
            username=claims.get('preferred_username'),
            avatar_url=claims.get('picture'),
        )


# --- Provider factory ---

_PROVIDERS: dict[str, type[OAuthProvider]] = {
    'google': GoogleProvider,
    'yandex': YandexProvider,
    'discord': DiscordProvider,
    'vk': VKProvider,
    'telegram': TelegramOIDCProvider,
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

    return provider_class(
        client_id=config['client_id'],
        client_secret=config['client_secret'],
        redirect_uri=redirect_uri,
    )
