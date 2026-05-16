"""Behavioral tests for the OAuth /callback email-merge branch.

The source-level pins in `test_oauth_revival_security.py` catch refactors
that delete the `user.email_verified` guard or compute `revived=` from
the post-mutation status. They do NOT catch a regression where the
guard is still in place but the surrounding logic stops actually firing
revival (e.g. someone changes the branch ordering or the user lookup).

This module exercises the email-merge logic at runtime by calling the
relevant branch directly with mocked I/O.

Also covers the security audit follow-up: when the local row exists
with `email_verified=False`, the endpoint must return a 409 instead of
falling through to `create_user_by_oauth` (which would crash with an
IntegrityError on the `User.email UNIQUE` constraint).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.oauth import oauth_callback
from app.database.models import UserStatus


def _user_info(
    *,
    provider_id: str = 'google-uid-42',
    email: str = 'alice@example.com',
    email_verified: bool = True,
) -> MagicMock:
    info = MagicMock()
    info.provider_id = provider_id
    info.email = email
    info.email_verified = email_verified
    info.first_name = 'Alice'
    info.last_name = 'Returner'
    info.username = 'alice'
    return info


def _local_user(
    *,
    user_id: int = 200,
    email: str = 'alice@example.com',
    email_verified: bool = True,
    status_value: str = UserStatus.DELETED.value,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=None,
        username=None,
        email=email,
        email_verified=email_verified,
        status=status_value,
        balance_kopeks=12345,
        last_activity=datetime(2024, 6, 1, tzinfo=UTC),
        updated_at=datetime(2024, 6, 1, tzinfo=UTC),
        referral_code='ALICE-OG',
        referred_by_id=None,
        remnawave_uuid=None,
    )


def _callback_request() -> MagicMock:
    req = MagicMock()
    req.code = 'auth-code'
    req.state = 'csrf-state'
    req.device_id = None
    req.campaign_slug = None
    req.referral_code = None
    return req


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    return session


def _common_oauth_patches(local_user_to_return: SimpleNamespace | None) -> list:
    """Patches shared across email-merge tests so we don't repeat 7 mocks per case."""
    return [
        patch('app.cabinet.routes.oauth.validate_oauth_state', AsyncMock(return_value={'linking': 'false'})),
        patch(
            'app.cabinet.routes.oauth.get_provider',
            return_value=MagicMock(
                exchange_code=AsyncMock(return_value={'access_token': 'tok'}),
                get_user_info=AsyncMock(return_value=_user_info()),
            ),
        ),
        # No provider_id match → falls into the email-merge branch (step 6).
        patch('app.cabinet.routes.oauth.get_user_by_oauth_provider', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.get_user_by_email', AsyncMock(return_value=local_user_to_return)),
        patch('app.cabinet.routes.oauth.set_user_oauth_provider_id', AsyncMock(return_value=None)),
        patch(
            'app.cabinet.routes.oauth._finalize_oauth_login',
            AsyncMock(return_value=MagicMock(name='AuthResponse')),
        ),
    ]


@pytest.mark.asyncio
async def test_email_merge_revives_deleted_user_when_both_verified(db: AsyncMock) -> None:
    """REGRESSION: with BOTH IdP and local row email_verified, a DELETED row gets revived.

    Pinned at source level by `test_email_merge_requires_local_user_email_verified`
    but that one doesn't actually call the endpoint. This test does, so
    a future ordering bug between the email_verified guard and the
    revive call shows up here.
    """
    deleted_user = _local_user(status_value=UserStatus.DELETED.value, email_verified=True)
    request = _callback_request()

    patches = _common_oauth_patches(local_user_to_return=deleted_user)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await oauth_callback(provider='google', request=request, db=db)

    assert deleted_user.status == UserStatus.ACTIVE.value, (
        'DELETED row found by verified-email match must be revived in-place — '
        'otherwise the cabinet creates a duplicate account on next login'
    )


@pytest.mark.asyncio
async def test_email_merge_blocks_409_when_local_email_unverified(db: AsyncMock) -> None:
    """SECURITY: local row with email_verified=False must NOT be merged.

    Pre-fix this fell through to create_user_by_oauth which hits the
    User.email UNIQUE constraint and 500s. The fix turns that into a
    clean 409 with `email_unverified_local` code so the frontend can
    guide the user to finish verification instead of staring at an
    opaque error.
    """
    unverified = _local_user(
        status_value=UserStatus.ACTIVE.value,
        email_verified=False,
    )
    request = _callback_request()

    patches = _common_oauth_patches(local_user_to_return=unverified)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        with pytest.raises(HTTPException) as exc:
            await oauth_callback(provider='google', request=request, db=db)

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail['code'] == 'email_unverified_local'
    # Status must NOT have been mutated (no takeover via IdP attestation alone).
    assert unverified.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_email_merge_active_user_links_without_revive(db: AsyncMock) -> None:
    """An ACTIVE local user found by email gets the provider linked, NOT revived.

    The `revived=` log field must reflect this (False), since the user
    was never DELETED. This pin protects the audit-log invariant that
    grepping for `revived=True` counts actual revivals.
    """
    active_user = _local_user(status_value=UserStatus.ACTIVE.value, email_verified=True)
    request = _callback_request()

    captured_logs: list[dict] = []

    def _log_capture(message: str, **kwargs: object) -> None:
        if message == 'OAuth provider linked to existing email user':
            captured_logs.append(dict(kwargs))

    patches = _common_oauth_patches(local_user_to_return=active_user)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch('app.cabinet.routes.oauth.logger.info', side_effect=_log_capture),
    ):
        await oauth_callback(provider='google', request=request, db=db)

    assert active_user.status == UserStatus.ACTIVE.value
    assert captured_logs, 'logger.info for the email-merge branch must fire'
    assert captured_logs[0].get('revived') is False, (
        'revived= must be False for ACTIVE users — audit log grep depends on this'
    )
