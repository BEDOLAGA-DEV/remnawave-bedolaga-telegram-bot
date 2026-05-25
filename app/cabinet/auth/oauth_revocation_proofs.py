from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from app.utils.cache import cache, cache_key

logger = structlog.get_logger(__name__)

OAUTH_REVOCATION_PROOF_TTL_SECONDS = 600
OAUTH_REVOCATION_PROOF_PREFIX = 'oauth_revocation_proof'
OAuthRevocationProofPurpose = Literal['unlink', 'delete']


async def create_oauth_revocation_proof(
    *,
    user_id: int,
    provider: str,
    provider_id: str,
    purpose: OAuthRevocationProofPurpose,
    event_id: int | None,
) -> str:
    token = secrets.token_urlsafe(32)
    data: dict[str, Any] = {
        'user_id': user_id,
        'provider': provider,
        'provider_id': provider_id,
        'purpose': purpose,
        'event_id': event_id,
        'created_at': datetime.now(UTC).isoformat(),
    }
    stored = await cache.set(cache_key(OAUTH_REVOCATION_PROOF_PREFIX, token), data, expire=OAUTH_REVOCATION_PROOF_TTL_SECONDS)
    if not stored:
        logger.error('Failed to store OAuth revocation proof', user_id=user_id, provider=provider)
        raise RuntimeError('Failed to store OAuth revocation proof')
    return token


async def get_oauth_revocation_proof(token: str) -> dict[str, Any] | None:
    data: Any = await cache.get(cache_key(OAUTH_REVOCATION_PROOF_PREFIX, token))
    if data is None or not isinstance(data, dict):
        return None
    return data


async def consume_oauth_revocation_proof(token: str) -> dict[str, Any] | None:
    data: Any = await cache.getdel(cache_key(OAUTH_REVOCATION_PROOF_PREFIX, token))
    if data is None or not isinstance(data, dict):
        return None
    return data
