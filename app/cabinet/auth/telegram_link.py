"""Utility for generating and validating Telegram linking tokens."""

from datetime import UTC, datetime, timedelta

import jwt

from app.config import settings

JWT_ALGORITHM = 'HS256'
LINK_TOKEN_EXPIRE_MINUTES = 10


def create_link_token(user_id: int) -> str:
    """
    Create a short-lived token for linking Telegram.

    Args:
        user_id: Database user ID to be linked

    Returns:
        Encoded JWT linking token
    """
    expires = datetime.now(UTC) + timedelta(minutes=LINK_TOKEN_EXPIRE_MINUTES)

    payload = {
        'sub': str(user_id),
        'type': 'telegram_link',
        'exp': expires,
        'iat': datetime.now(UTC),
    }

    secret = settings.get_cabinet_jwt_secret()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_link_token(token: str) -> int | None:
    """
    Decode linking token and return user_id.

    Args:
        token: JWT linking token string

    Returns:
        Database user ID or None if token is invalid or expired
    """
    try:
        secret = settings.get_cabinet_jwt_secret()
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])

        if payload.get('type') != 'telegram_link':
            return None

        return int(payload['sub'])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, ValueError, KeyError):
        return None
