"""Linking an email that a DELETED account still holds.

Inactive-user cleanup only flips ``status`` to DELETED and leaves the email in place,
while the unique index on users.email still covers that row. POST /email/register
skipped deleted rows in its "is it taken?" check and crashed with a 500 on the
UPDATE — for any password, on every attempt.

Now the deleted holder goes through the same emailed code as a live one, and the
address moves to the caller only after the code is confirmed. Nothing is released
on the strength of just knowing the address.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.cabinet.routes.auth import register_email, verify_email_merge
from app.cabinet.schemas.auth import EmailMergeVerifyRequest, EmailRegisterRequest
from app.database.models import UserStatus


EMAIL = 'returning@example.com'
CODE = '654321'


def _result(*, one: object = None, rows: list | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = one
    result.scalars.return_value.first.return_value = rows[0] if rows else None
    result.scalars.return_value.all.return_value = rows or []
    return result


def _db(*results: MagicMock) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


def _caller(**overrides) -> SimpleNamespace:
    fields = {
        'id': 1,
        'email': None,
        'email_verified': False,
        'password_hash': None,
        'language': 'en',
        'first_name': 'Eva',
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _deleted_holder() -> SimpleNamespace:
    return SimpleNamespace(
        id=2,
        email=EMAIL,
        email_verified=True,
        email_verified_at='2026-02-21',
        email_verification_source='cabinet',
        email_verification_token=None,
        email_verification_expires=None,
        password_hash='old-hash',
        status=UserStatus.DELETED.value,
    )


def _live_holder() -> SimpleNamespace:
    return SimpleNamespace(id=3, email=EMAIL, email_verified=True, status=UserStatus.ACTIVE.value)


def _auth(name: str, value: object):
    return patch(f'app.cabinet.routes.auth.{name}', value)


def _common(s: ExitStack) -> None:
    s.enter_context(_auth('get_client_ip', MagicMock(return_value='1.2.3.4')))
    s.enter_context(_auth('RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))


@pytest.fixture(autouse=True)
def _email_auth_enabled(monkeypatch):
    from app.cabinet.auth import email_auth_gate

    monkeypatch.setattr(email_auth_gate, 'get_setting_value', AsyncMock(return_value=None))
    monkeypatch.setattr(email_auth_gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)


async def _register(db: AsyncMock, store: AsyncMock, user: SimpleNamespace | None = None) -> dict:
    with ExitStack() as s:
        _common(s)
        s.enter_context(_auth('disposable_email_service.is_disposable', MagicMock(return_value=False)))
        s.enter_context(_auth('email_service.is_configured', MagicMock(return_value=True)))
        s.enter_context(_auth('email_service.send_email_change_code', MagicMock()))
        s.enter_context(_auth('get_rendered_override', AsyncMock(return_value=None)))
        s.enter_context(_auth('hash_password', MagicMock(return_value='new-hash')))
        s.enter_context(_auth('store_email_merge_otp', store))
        return await register_email(
            request=EmailRegisterRequest(email=EMAIL, password='new-password'),
            raw_request=MagicMock(),
            user=user or _caller(),
            db=db,
        )


async def _verify(db: AsyncMock, pending: dict, secondary: object, user: SimpleNamespace, sync: AsyncMock) -> dict:
    with ExitStack() as s:
        _common(s)
        s.enter_context(_auth('get_email_merge_otp', AsyncMock(return_value=pending)))
        s.enter_context(_auth('clear_email_merge_otp', AsyncMock()))
        s.enter_context(_auth('get_user_by_id', AsyncMock(return_value=secondary)))
        s.enter_context(_auth('create_merge_token', AsyncMock(side_effect=AssertionError('no merge for deleted'))))
        s.enter_context(_auth('_sync_subscription_from_panel_by_email', sync))
        return await verify_email_merge(
            request=EmailMergeVerifyRequest(code=CODE),
            raw_request=MagicMock(),
            user=user,
            db=db,
        )


def _pending(**overrides) -> dict:
    pending = {'secondary_user_id': 2, 'email': EMAIL, 'code': CODE, 'password_hash': 'new-hash'}
    pending.update(overrides)
    return pending


@pytest.mark.asyncio
async def test_register_sends_code_when_deleted_account_holds_email() -> None:
    """No 500 and no release yet — a code goes to the inbox, the deleted row is untouched."""
    holder = _deleted_holder()
    store = AsyncMock()
    db = _db(_result(one=None), _result(rows=[holder]))

    result = await _register(db, store)

    assert result == {
        'message': 'A confirmation code was sent to that email address.',
        'merge_required': True,
        'merge_verification': 'email_code',
        'merge_token': None,
    }
    store.assert_awaited_once()
    assert store.await_args.args[1] == holder.id
    assert store.await_args.kwargs['password_hash'] == 'new-hash'
    assert holder.email == EMAIL
    assert holder.password_hash == 'old-hash'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_live_holder_does_not_keep_password_hash() -> None:
    """The live-account merge path is unchanged: nothing extra is parked in the cache."""
    store = AsyncMock()
    db = _db(_result(one=_live_holder()))

    await _register(db, store)

    assert store.await_args.kwargs['password_hash'] is None


@pytest.mark.asyncio
async def test_register_race_on_free_email_returns_409() -> None:
    """Another account grabbed the address between the check and the write."""
    db = _db(_result(one=None), _result(rows=[]))
    db.flush = AsyncMock(side_effect=IntegrityError('UPDATE users', {}, Exception('duplicate key')))

    with pytest.raises(HTTPException) as exc:
        await _register(db, AsyncMock())

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert 'already registered' in exc.value.detail
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_moves_email_from_deleted_account() -> None:
    holder = _deleted_holder()
    user = _caller(password_reset_token='stale', password_reset_expires='later')
    sync = AsyncMock()
    db = _db(_result(rows=[holder]))

    result = await _verify(db, _pending(), holder, user, sync)

    assert result['email_linked'] is True
    assert result['merge_required'] is False
    assert (holder.email, holder.email_verified, holder.password_hash) == (None, False, None)
    assert user.email == EMAIL
    assert user.email_verified is True
    assert user.email_verification_source == 'cabinet'
    assert user.password_hash == 'new-hash'
    assert user.password_reset_token is None
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()
    sync.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_verify_wrong_code_leaves_deleted_account_alone() -> None:
    holder = _deleted_holder()
    user = _caller()
    db = _db()

    with pytest.raises(HTTPException) as exc:
        with ExitStack() as s:
            _common(s)
            s.enter_context(_auth('get_email_merge_otp', AsyncMock(return_value=_pending())))
            s.enter_context(_auth('get_user_by_id', AsyncMock(return_value=holder)))
            await verify_email_merge(
                request=EmailMergeVerifyRequest(code='111111'),
                raw_request=MagicMock(),
                user=user,
                db=db,
            )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert holder.email == EMAIL
    assert user.email is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_refuses_when_address_came_back_to_life() -> None:
    """Revived while the code was in flight — that is a live account and needs a real merge."""
    holder = _deleted_holder()
    user = _caller()
    db = _db(_result(rows=[holder, _live_holder()]))

    with pytest.raises(HTTPException) as exc:
        await _verify(db, _pending(), holder, user, AsyncMock())

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert holder.email == EMAIL
    assert user.email is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_refuses_when_caller_already_has_verified_email() -> None:
    holder = _deleted_holder()
    user = _caller(email='other@example.com', email_verified=True)
    db = _db(_result(rows=[holder]))

    with pytest.raises(HTTPException) as exc:
        await _verify(db, _pending(), holder, user, AsyncMock())

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert holder.email == EMAIL
    assert user.email == 'other@example.com'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_without_parked_password_keeps_callers_password() -> None:
    """Holder was live when the code was sent and got deleted before it was entered."""
    holder = _deleted_holder()
    user = _caller(password_hash='callers-own')
    db = _db(_result(rows=[holder]))

    await _verify(db, _pending(password_hash=None), holder, user, AsyncMock())

    assert user.email == EMAIL
    assert user.password_hash == 'callers-own'
