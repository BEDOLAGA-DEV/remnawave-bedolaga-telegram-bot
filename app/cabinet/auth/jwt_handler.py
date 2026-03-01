"""JWT token handling for cabinet authentication."""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config import settings


JWT_ALGORITHM = 'HS256'


def create_access_token(
    user_id: int,
    telegram_id: int | None = None,
    *,
    permissions: list[str] | None = None,
    roles: list[str] | None = None,
    role_level: int = 0,
) -> str:
    """
    Create a short-lived access token.

    Args:
        user_id: Database user ID
        telegram_id: Telegram user ID (optional for email-only users)
        permissions: RBAC permission strings to embed in token
        roles: Role names to embed in token
        role_level: Maximum role level (0 = no special level)

    Returns:
        Encoded JWT access token
    """
    expire_minutes = settings.get_cabinet_access_token_expire_minutes()
    expires = datetime.now(UTC) + timedelta(minutes=expire_minutes)

    payload = {
        'sub': str(user_id),
        'type': 'access',
        'exp': expires,
        'iat': datetime.now(UTC),
    }

    # Добавляем telegram_id только если он есть
    if telegram_id is not None:
        payload['telegram_id'] = telegram_id

    # RBAC data — only include when provided to keep token compact
    if permissions is not None:
        payload['permissions'] = permissions
    if roles is not None:
        payload['roles'] = roles
    if role_level > 0:
        payload['role_level'] = role_level

    secret = settings.get_cabinet_jwt_secret()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: int) -> str:
    """
    Create a long-lived refresh token.

    Args:
        user_id: Database user ID

    Returns:
        Encoded JWT refresh token
    """
    expire_days = settings.get_cabinet_refresh_token_expire_days()
    expires = datetime.now(UTC) + timedelta(days=expire_days)

    payload = {
        'sub': str(user_id),
        'type': 'refresh',
        'exp': expires,
        'iat': datetime.now(UTC),
    }

    secret = settings.get_cabinet_jwt_secret()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """
    Decode and validate a JWT token.

    Args:
        token: JWT token string

    Returns:
        Decoded payload dict or None if invalid/expired
    """
    try:
        secret = settings.get_cabinet_jwt_secret()
        return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_token_payload(token: str, expected_type: str = 'access') -> dict[str, Any] | None:
    """
    Decode token and verify its type.

    Args:
        token: JWT token string
        expected_type: Expected token type ("access" or "refresh")

    Returns:
        Decoded payload dict or None if invalid/expired/wrong type
    """
    payload = decode_token(token)

    if not payload:
        return None

    if payload.get('type') != expected_type:
        return None

    return payload


def get_refresh_token_expires_at() -> datetime:
    """Get the expiration datetime for a new refresh token."""
    expire_days = settings.get_cabinet_refresh_token_expire_days()
    return datetime.now(UTC) + timedelta(days=expire_days)
