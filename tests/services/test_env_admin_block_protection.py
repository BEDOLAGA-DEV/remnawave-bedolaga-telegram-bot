"""An account named in ADMIN_IDS/ADMIN_EMAILS must never end up BLOCKED.

BLOCKED is not only an administrative punishment in this codebase: a broadcast
delivery that comes back "user blocked the bot" and the blocked-users scan both set
it on their own. Without these guards an owner who merely muted the bot would lose
access to the bot and the cabinet with no way back in.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.database.models import UserStatus


@pytest.fixture
def env_admin(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [42])
    monkeypatch.setattr(type(settings), 'get_admin_emails', lambda self: [])
    return SimpleNamespace(id=7, telegram_id=42, email=None, email_verified=False, status=UserStatus.ACTIVE.value)


def test_predicate_covers_admin_ids_and_admin_emails(monkeypatch):
    from app.config import settings
    from app.services.rbac_bootstrap_service import is_protected_from_blocking

    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [42])
    monkeypatch.setattr(type(settings), 'get_admin_emails', lambda self: ['owner@example.com'])

    by_id = SimpleNamespace(telegram_id=42, email=None, email_verified=False)
    by_email = SimpleNamespace(
        telegram_id=99,
        email='owner@example.com',
        email_verified=True,
        email_verification_source=None,
    )
    ordinary = SimpleNamespace(telegram_id=99, email='someone@example.com', email_verified=True)

    assert is_protected_from_blocking(by_id) is True
    assert is_protected_from_blocking(by_email) is True
    assert is_protected_from_blocking(ordinary) is False


@pytest.mark.asyncio
async def test_admin_panel_ban_refuses_env_admin(env_admin, monkeypatch):
    from app.services import user_service as user_service_module

    env_admin.remnawave_id = None
    env_admin.subscriptions = []
    update_user = AsyncMock()
    monkeypatch.setattr(user_service_module, 'get_user_by_id', AsyncMock(return_value=env_admin))
    monkeypatch.setattr(user_service_module, 'update_user', update_user)

    blocked = await user_service_module.UserService().block_user(AsyncMock(), env_admin.id, admin_id=1)

    assert blocked is False
    update_user.assert_not_awaited()
    assert env_admin.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_cabinet_disable_user_refuses_env_admin(env_admin):
    from app.cabinet.routes import admin_users

    with patch.object(admin_users, 'get_user_by_id', AsyncMock(return_value=env_admin)):
        with pytest.raises(HTTPException) as refused:
            await admin_users.disable_user(
                env_admin.id,
                admin_users.DisableUserRequest(),
                admin=SimpleNamespace(id=1),
                db=AsyncMock(),
            )

    assert refused.value.status_code == 403
    assert env_admin.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_broadcast_auto_block_skips_env_admin(env_admin):
    """A muted bot reports the owner as "blocked" — that must not flip their status."""
    from app.services import broadcast_service

    class _Result:
        def scalar_one_or_none(self):
            return env_admin

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch.object(broadcast_service, 'AsyncSessionLocal', lambda: session):
        await broadcast_service.cleanup_blocked_broadcast_users([env_admin.telegram_id])

    assert env_admin.status == UserStatus.ACTIVE.value
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_users_scan_skips_env_admin(env_admin):
    from app.services.blocked_users_service import BlockedUsersService

    class _Result:
        def scalar_one_or_none(self):
            return env_admin

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())

    marked = await BlockedUsersService(bot=AsyncMock()).mark_user_as_blocked(db, env_admin.id)

    assert marked is False
    assert env_admin.status == UserStatus.ACTIVE.value
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cabinet_restores_stale_blocked_admin_with_invite_only_off(env_admin):
    """The gate only emits VERIFIED_ADMIN while invite-only is on — recovery must not depend on it."""
    from app.cabinet.auth.registration_access import is_env_admin_recovery
    from app.cabinet.routes import auth
    from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason

    env_admin.status = UserStatus.BLOCKED.value
    invite_only_off = RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_ONLY_DISABLED)

    assert is_env_admin_recovery(env_admin, invite_only_off) is True

    await auth._recover_cabinet_user_after_gate(
        AsyncMock(), env_admin, invite_only_off, source='cabinet_telegram_login'
    )

    assert env_admin.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_cabinet_still_refuses_a_blocked_ordinary_user(monkeypatch):
    from app.cabinet.routes import auth
    from app.config import settings
    from app.services.registration_access_service import RegistrationAccessDecision, RegistrationAccessReason

    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [42])
    monkeypatch.setattr(type(settings), 'get_admin_emails', lambda self: [])
    user = SimpleNamespace(status=UserStatus.BLOCKED.value, telegram_id=43, email=None, email_verified=False)

    with pytest.raises(HTTPException) as refused:
        await auth._recover_cabinet_user_after_gate(
            AsyncMock(),
            user,
            RegistrationAccessDecision(True, RegistrationAccessReason.INVITE_ONLY_DISABLED),
            source='cabinet_telegram_login',
        )

    assert refused.value.status_code == 403
    assert user.status == UserStatus.BLOCKED.value


def test_middleware_heals_the_stale_blocked_flag_on_an_env_admin():
    """Letting the admin through is not enough — the flag also suppresses reactivation."""
    from pathlib import Path

    from app.middlewares import auth

    source = Path(auth.__file__).read_text(encoding='utf-8')
    healed = source.split('if _is_blocked_non_admin(db_user):', 1)[1]

    assert 'db_user.status = UserStatus.ACTIVE.value' in healed.split('UserStatus.DELETED.value', 1)[0]
