"""Unit tests for `app.services.user_revival_service.revive_deleted_user`.

These tests pin the **narrow** revival contract used by the cabinet:
  * flip status DELETED → ACTIVE
  * touch last_activity + updated_at
  * leave everything else (balance, subs, referrals, consent) intact
  * raise NotDeletedError if called on a non-DELETED row

The bot's clean-slate revival in `handlers/start.py` is a different code
path (full wipe before re-registration) — these tests do NOT cover it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.database.models import UserStatus
from app.services.user_revival_service import NotDeletedError, revive_deleted_user


class _FakeUser:
    """Bare-bones stand-in for the User ORM model.

    We deliberately avoid spinning up SQLAlchemy here — the service is a
    pure mutator + commit + log, and a struct-style stub keeps the unit
    test honest and fast.
    """

    def __init__(self, **kwargs: object) -> None:
        self.id = kwargs.get('id', 42)
        self.telegram_id = kwargs.get('telegram_id', 12345)
        self.username = kwargs.get('username', 'someone')
        self.email = kwargs.get('email')
        self.email_verified = bool(kwargs.get('email_verified', False))
        self.status = kwargs.get('status', UserStatus.DELETED.value)
        self.balance_kopeks = kwargs.get('balance_kopeks', 50000)
        self.last_activity = kwargs.get('last_activity', datetime(2025, 1, 1, tzinfo=UTC))
        self.updated_at = kwargs.get('updated_at', datetime(2025, 1, 1, tzinfo=UTC))
        # We track these to assert the revival is NON-destructive.
        self.referral_code = kwargs.get('referral_code', 'INV-XYZ')
        self.referred_by_id = kwargs.get('referred_by_id', 7)
        self.remnawave_uuid = kwargs.get('remnawave_uuid', 'panel-uuid-abc')


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    return session


@pytest.mark.asyncio
async def test_revive_flips_status_to_active(db: AsyncMock) -> None:
    user = _FakeUser(status=UserStatus.DELETED.value)
    before = user.updated_at

    result = await revive_deleted_user(db, user, source='unit_test')

    assert result is user
    assert user.status == UserStatus.ACTIVE.value
    assert user.last_activity > before
    assert user.updated_at > before


@pytest.mark.asyncio
async def test_revive_preserves_balance_and_referral_state(db: AsyncMock) -> None:
    """The cabinet revival path is NOT a wipe — value-bearing fields stay."""
    user = _FakeUser(balance_kopeks=99999, referral_code='KEEP-ME', referred_by_id=11)

    await revive_deleted_user(db, user, source='unit_test')

    assert user.balance_kopeks == 99999, 'balance must not be zeroed on cabinet revival'
    assert user.referral_code == 'KEEP-ME', 'referral_code must not be regenerated on cabinet revival'
    assert user.referred_by_id == 11, 'referrer attribution must survive cabinet revival'
    assert user.remnawave_uuid == 'panel-uuid-abc', 'panel UUID must not be cleared'


@pytest.mark.asyncio
async def test_revive_never_commits_caller_owns_transaction(db: AsyncMock) -> None:
    """Architect's call: revive_deleted_user must NEVER commit.

    Caller-owns-transaction is the only rule. This kills the
    log-vs-state inconsistency we used to have when commit=False
    callers' downstream commit raised after the audit line was emitted.
    """
    user = _FakeUser()
    await revive_deleted_user(db, user, source='unit_test')
    db.commit.assert_not_called()
    db.refresh.assert_not_called()
    # State is flipped in-place — caller's commit persists it.
    assert user.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_revive_raises_when_already_active(db: AsyncMock) -> None:
    """Misuse-guard: revive must NEVER silently no-op on ACTIVE rows."""
    user = _FakeUser(status=UserStatus.ACTIVE.value)
    with pytest.raises(NotDeletedError):
        await revive_deleted_user(db, user, source='unit_test')
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_revive_raises_on_blocked_user(db: AsyncMock) -> None:
    """A BLOCKED admin-action row is a separate domain — revival is wrong here."""
    user = _FakeUser(status=UserStatus.BLOCKED.value)
    with pytest.raises(NotDeletedError):
        await revive_deleted_user(db, user, source='unit_test')
    db.commit.assert_not_called()
