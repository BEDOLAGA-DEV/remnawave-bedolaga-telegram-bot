"""Integration-style tests for DELETED-user revival in cabinet auth.

Covers the three places where revival now fires:
  1. `cabinet/dependencies.py::get_current_cabinet_user` — auto-revive
     when a signed Telegram initData proves identity.
  2. `cabinet/routes/auth.py` `/email/login` — must NOT silently revive
     (no Telegram signature to prove identity); must return the
     structured `account_deleted` error code so the frontend can show
     the friendly screen with bot deep-link.
  3. `cabinet/routes/oauth.py` callback — OAuth provider_id / verified
     email match is identity proof; revive instead of creating a
     duplicate account.

Each test mocks the surrounding I/O (Telegram signature validator,
JWT decoder, DB session) so we can lean on plain pytest-asyncio without
a Postgres testcontainer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.dependencies import get_current_cabinet_user
from app.database.models import UserStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    *,
    user_id: int = 100,
    telegram_id: int = 555,
    status_value: str = UserStatus.DELETED.value,
    email: str | None = None,
    email_verified: bool = False,
    username: str = 'returning_user',
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        username=username,
        email=email,
        email_verified=email_verified,
        status=status_value,
        balance_kopeks=0,
        last_activity=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        # Touched at the end of get_current_cabinet_user even on a
        # successful revive — stub must have the attribute or
        # SimpleNamespace blows up before any assertion runs.
        cabinet_last_login=None,
        referral_code='abc',
        referred_by_id=None,
        remnawave_uuid=None,
    )


def _make_request(init_data: str | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = MagicMock()
    req.headers.get = MagicMock(side_effect=lambda key: init_data if key == 'X-Telegram-Init-Data' else None)
    return req


def _credentials(token: str = 'fake.jwt.token') -> MagicMock:  # noqa: S107 — pytest fixture sentinel, not a real secret
    return MagicMock(credentials=token)


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    return session


# ---------------------------------------------------------------------------
# dependencies.py: auto-revive via initData
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependencies_auto_revives_deleted_user_with_valid_init_data(db: AsyncMock) -> None:
    """REGRESSION: signed initData proving same telegram_id → revive in place."""
    user = _make_user(status_value=UserStatus.DELETED.value, telegram_id=555)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.validate_telegram_init_data',
            return_value={'id': 555, 'username': 'returning_user'},
        ),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(init_data='valid-signed-init-data'),
            credentials=_credentials(),
            db=db,
        )

    assert result is user
    assert user.status == UserStatus.ACTIVE.value, 'DELETED user must be flipped to ACTIVE'


@pytest.mark.asyncio
async def test_dependencies_rejects_deleted_user_without_init_data(
    db: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a fresh signature, return structured 403 — never auto-revive."""
    user = _make_user(status_value=UserStatus.DELETED.value)

    # `settings.get_bot_username()` reads from `settings.BOT_USERNAME`.
    # Patch the underlying field via monkeypatch — pydantic BaseSettings
    # allows that, but rejects patching arbitrary method names directly.
    from app.cabinet.dependencies import settings as deps_settings

    monkeypatch.setattr(deps_settings, 'BOT_USERNAME', 'mybot', raising=False)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(init_data=None),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail['code'] == 'account_deleted'
    assert detail['bot_username'] == 'mybot'
    assert detail['telegram_deep_link'] == 'https://t.me/mybot?start=revive'
    assert user.status == UserStatus.DELETED.value, 'must not auto-revive without proof'


@pytest.mark.asyncio
async def test_dependencies_rejects_deleted_user_with_mismatched_init_data(db: AsyncMock) -> None:
    """initData proving DIFFERENT telegram_id → cross-account 401, NOT revival.

    This is the exact attack vector the cross-account guard already
    blocks; we just check the revival branch doesn't accidentally
    overrun it for DELETED users.
    """
    user = _make_user(status_value=UserStatus.DELETED.value, telegram_id=555)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.validate_telegram_init_data',
            return_value={'id': 9999, 'username': 'attacker'},
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(init_data='signed-but-different-account'),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED, (
        'Cross-account guard must trip BEFORE the revival branch — '
        'otherwise a signed initData from any account could revive any DELETED row'
    )
    assert user.status == UserStatus.DELETED.value


@pytest.mark.asyncio
async def test_dependencies_blocks_revival_for_blacklisted_deleted_user(db: AsyncMock) -> None:
    """A DELETED + blacklisted row must NOT be revived. Banned stays banned.

    Blacklist runs BEFORE the status branch (security audit fix), so the
    initData being present is irrelevant — the blacklist 403 fires
    regardless. Same expectation as before; just that the path is now
    independent of the initData branch.
    """
    user = _make_user(status_value=UserStatus.DELETED.value, telegram_id=555)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.validate_telegram_init_data',
            return_value={'id': 555, 'username': 'returning_user'},
        ),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(True, 'Spam abuse')),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(init_data='valid-signed-init-data'),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail['code'] == 'blacklisted'
    assert user.status == UserStatus.DELETED.value, 'banned user must stay DELETED'


@pytest.mark.asyncio
async def test_dependencies_blacklist_runs_before_status_check_for_no_init_data(db: AsyncMock) -> None:
    """REGRESSION: blacklisted+DELETED without initData must still return
    blacklisted code, NOT the friendly account_deleted screen.

    Pre-fix order was: status check → friendly account_deleted reply →
    blacklist (never reached). This leaked the recoverable-account hint
    to banned spammers. Post-fix the blacklist runs first.
    """
    user = _make_user(status_value=UserStatus.DELETED.value, telegram_id=555)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(True, 'Fraud')),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(init_data=None),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail['code'] == 'blacklisted', (
        "blacklist must take precedence over account_deleted so banned users don't see the friendly revive screen"
    )


@pytest.mark.asyncio
async def test_dependencies_preserves_blocked_status_with_generic_message(db: AsyncMock) -> None:
    """Status=BLOCKED is an admin action, not inactivity — generic 403."""
    user = _make_user(status_value=UserStatus.BLOCKED.value)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(init_data=None),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    # The BLOCKED branch returns the plain string detail (the friendly
    # screen is reserved for the DELETED case where the user has a
    # concrete recovery action).
    assert exc.value.detail == 'User account is not active'


@pytest.mark.asyncio
async def test_dependencies_active_user_still_passes_through(db: AsyncMock) -> None:
    """Negative-control: ACTIVE user is unaffected by all the new branches.

    Blacklist mock added explicitly: post-fix the check now runs BEFORE
    the status branch, so the previous "no mock = MagicMock truthy"
    false positive is a real risk if we don't pin it.
    """
    user = _make_user(status_value=UserStatus.ACTIVE.value)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(init_data=None),
            credentials=_credentials(),
            db=db,
        )

    assert result is user
    assert user.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_dependencies_auto_revive_persists_via_db_commit(db: AsyncMock) -> None:
    """Pin the caller-owns-commit contract at the dependency boundary.

    `revive_deleted_user` no longer commits — the caller (the dependency
    here) must. If a refactor accidentally drops the commit, the in-
    memory status flip is lost on process restart and we silently
    return to the bug we set out to fix.
    """
    user = _make_user(status_value=UserStatus.DELETED.value, telegram_id=555)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.validate_telegram_init_data',
            return_value={'id': 555, 'username': 'returning_user'},
        ),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
    ):
        await get_current_cabinet_user(
            request=_make_request(init_data='valid-signed-init-data'),
            credentials=_credentials(),
            db=db,
        )

    db.commit.assert_awaited()
    db.refresh.assert_awaited()


@pytest.mark.asyncio
async def test_dependencies_rejects_deleted_user_with_invalid_init_data(
    db: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """initData header present but signature INVALID → falls back to no-proof path.

    `validate_telegram_init_data` returns None on signature/age failure.
    `init_data_matches_user` stays False, so revival is skipped and we
    fall through to the friendly account_deleted 403. This is the
    branch distinct from "no header at all" (#11) — the validator-
    failure case can drift independently and deserves its own pin.
    """
    user = _make_user(status_value=UserStatus.DELETED.value, telegram_id=555)
    from app.cabinet.dependencies import settings as deps_settings

    monkeypatch.setattr(deps_settings, 'BOT_USERNAME', 'mybot', raising=False)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        # Validator says "nope" — tampered or expired initData.
        patch('app.cabinet.dependencies.validate_telegram_init_data', return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(init_data='tampered-or-expired'),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail['code'] == 'account_deleted'
    assert user.status == UserStatus.DELETED.value


@pytest.mark.asyncio
async def test_dependencies_deleted_email_only_user_without_telegram_id(
    db: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Email-only DELETED user (telegram_id=None) → 403 account_deleted, no AttributeError.

    Verifies the response builder doesn't fall over when telegram_id is
    None: deep_link still constructs (it doesn't depend on the user's
    telegram_id, just on the configured BOT_USERNAME), and the blacklist
    short-circuit (which is keyed on telegram_id) is skipped cleanly.
    """
    user = _make_user(status_value=UserStatus.DELETED.value, telegram_id=None)
    from app.cabinet.dependencies import settings as deps_settings

    monkeypatch.setattr(deps_settings, 'BOT_USERNAME', 'mybot', raising=False)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '100', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(init_data=None),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail['code'] == 'account_deleted'
    # The deep-link is still emitted — it points to the bot itself, not
    # to any specific user. An email-only user CAN go open the bot via
    # /start with their referral chain to bootstrap.
    assert detail['telegram_deep_link'] == 'https://t.me/mybot?start=revive'
    assert user.status == UserStatus.DELETED.value
