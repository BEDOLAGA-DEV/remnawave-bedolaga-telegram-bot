"""Telegram authentication validation for cabinet."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx
import jwt as pyjwt
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


# Maximum allowed clock skew (seconds) for auth_date — tolerates minor drift between Telegram servers and ours.
_MAX_CLOCK_SKEW_SECONDS = 300


def validate_telegram_login_widget(data: dict[str, Any], max_age_seconds: int = 86400) -> bool:
    """
    Validate Telegram Login Widget data.

    https://core.telegram.org/widgets/login#checking-authorization

    Args:
        data: Dictionary with Telegram login data (id, first_name, auth_date, hash, etc.)
        max_age_seconds: Maximum allowed age of auth_date (default 24 hours)

    Returns:
        True if data is valid, False otherwise
    """
    auth_data = data.copy()
    check_hash = auth_data.pop('hash', None)

    if not check_hash:
        return False

    # Check auth_date is present and within valid range
    auth_date = auth_data.get('auth_date')
    if not auth_date:
        return False
    try:
        auth_time = datetime.fromtimestamp(int(auth_date), tz=UTC)
        age = (datetime.now(UTC) - auth_time).total_seconds()
        if age > max_age_seconds or age < -_MAX_CLOCK_SKEW_SECONDS:
            return False
    except (ValueError, TypeError, OSError):
        return False

    # Build data-check-string (sorted key=value pairs, newline-separated)
    data_check_arr = [f'{k}={v}' for k, v in sorted(auth_data.items()) if v is not None]
    data_check_string = '\n'.join(data_check_arr)

    # Create secret key from bot token using SHA256
    bot_token = settings.BOT_TOKEN
    secret_key = hashlib.sha256(bot_token.encode()).digest()

    # Calculate expected hash
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    return hmac.compare_digest(calculated_hash, check_hash)


def validate_telegram_init_data(init_data: str, max_age_seconds: int = 86400) -> dict[str, Any] | None:
    """
    Validate Telegram WebApp initData.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Args:
        init_data: Raw initData string from Telegram WebApp
        max_age_seconds: Maximum allowed age of auth_date (default 24 hours)

    Returns:
        Parsed user data dict if valid, None otherwise
    """
    try:
        # Parse the init_data string
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))

        received_hash = parsed.pop('hash', None)
        if not received_hash:
            return None

        # Check auth_date is present and within valid range
        auth_date = parsed.get('auth_date')
        if not auth_date:
            return None
        try:
            auth_time = datetime.fromtimestamp(int(auth_date), tz=UTC)
            age = (datetime.now(UTC) - auth_time).total_seconds()
            if age > max_age_seconds or age < -_MAX_CLOCK_SKEW_SECONDS:
                return None
        except (ValueError, TypeError, OSError):
            return None

        # Build data-check-string
        data_check_arr = [f'{k}={v}' for k, v in sorted(parsed.items())]
        data_check_string = '\n'.join(data_check_arr)

        # Create secret key: HMAC_SHA256(bot_token, "WebAppData")
        bot_token = settings.BOT_TOKEN
        secret_key = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()

        # Calculate expected hash
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        # Parse user data from the validated data
        user_data_str = parsed.get('user')
        if user_data_str:
            user_data = json.loads(user_data_str)
            return user_data

        return parsed

    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def extract_telegram_user_from_init_data(init_data: str) -> dict[str, Any] | None:
    """
    Extract and validate user info from Telegram WebApp initData.

    Args:
        init_data: Raw initData string from Telegram WebApp

    Returns:
        User data dict with id, first_name, last_name, username, etc. or None if invalid
    """
    return validate_telegram_init_data(init_data)


# JWKS cache (module-level, refreshed periodically)
_jwks_cache: dict[str, Any] = {}
_jwks_cache_expiry: datetime | None = None
_JWKS_CACHE_TTL_SECONDS = 3600  # 1 hour
_JWKS_URL = 'https://oauth.telegram.org/.well-known/jwks.json'
_OIDC_ISSUER = 'https://oauth.telegram.org'

_jwks_lock = asyncio.Lock()
_jwks_last_force_refresh: datetime | None = None
_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS = 30


def _build_public_keys(jwks_data: dict[str, Any]) -> dict[str, Any]:
    """Build public key mapping from JWKS data."""
    public_keys: dict[str, Any] = {}
    for key_data in jwks_data.get('keys', []):
        kid = key_data.get('kid')
        if kid:
            public_keys[kid] = pyjwt.algorithms.RSAAlgorithm.from_jwk(key_data)
    return public_keys


async def _get_jwks(force: bool = False) -> dict[str, Any]:
    """Fetch and cache Telegram OIDC JWKS keys."""
    global _jwks_cache, _jwks_cache_expiry

    now = datetime.now(UTC)
    if not force and _jwks_cache and _jwks_cache_expiry and now < _jwks_cache_expiry:
        return _jwks_cache

    async with _jwks_lock:
        # Double-check after acquiring lock
        now = datetime.now(UTC)
        if not force and _jwks_cache and _jwks_cache_expiry and now < _jwks_cache_expiry:
            return _jwks_cache

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(_JWKS_URL)
            response.raise_for_status()
            _jwks_cache = response.json()
            _jwks_cache_expiry = now + timedelta(seconds=_JWKS_CACHE_TTL_SECONDS)
            return _jwks_cache


async def _force_refresh_jwks(kid: str) -> dict[str, Any] | None:
    """Force JWKS refresh with cooldown protection. Returns refreshed JWKS or None if on cooldown."""
    global _jwks_cache_expiry, _jwks_last_force_refresh

    async with _jwks_lock:
        now = datetime.now(UTC)
        if (
            _jwks_last_force_refresh
            and (now - _jwks_last_force_refresh).total_seconds() < _JWKS_FORCE_REFRESH_COOLDOWN_SECONDS
        ):
            logger.warning('Telegram OIDC: JWKS force refresh on cooldown', kid=kid)
            return None
        _jwks_last_force_refresh = now
        _jwks_cache_expiry = None

    return await _get_jwks(force=True)


async def validate_telegram_oidc_token(id_token: str, client_id: str) -> dict[str, Any] | None:
    """
    Validate a Telegram OIDC id_token using JWKS.

    Args:
        id_token: JWT id_token from Telegram OIDC flow
        client_id: Expected audience (bot's numeric ID as string)

    Returns:
        Decoded claims dict if valid, None otherwise.
        Claims include: sub, id, name, preferred_username, picture, iss, aud, exp, iat
    """
    try:
        # Build public keys from JWKS
        jwks_data = await _get_jwks()
        public_keys = _build_public_keys(jwks_data)

        # Decode header to get kid
        unverified_header = pyjwt.get_unverified_header(id_token)
        kid = unverified_header.get('kid')

        # If kid not found, force JWKS refresh (key rotation) with cooldown
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
