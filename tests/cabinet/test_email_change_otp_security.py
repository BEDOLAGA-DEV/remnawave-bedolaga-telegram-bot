"""Security: the email-change OTP must not be brute-forceable or bind admin emails.

The 6-digit code is mailed only to the NEW address (the attacker never sees it),
so without rate limiting + a per-account cap an attacker could enumerate the
900k space within the 15-min TTL to take over an arbitrary unregistered email —
including an ADMIN_EMAILS address, which auto-grants superadmin on next login.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.auth import request_email_change, verify_email_change
from app.cabinet.schemas.auth import EmailChangeRequest, EmailChangeVerifyRequest
from app.config import settings
from app.database.crud.user import verify_and_apply_email_change


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, email='owner@example.com', email_verified=True)


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock(return_value=None)
    db.refresh = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_verify_blocked_and_code_not_checked_when_ip_rate_limited() -> None:
    verify_apply = AsyncMock(return_value=(True, 'ok'))
    with ExitStack() as stack:
        stack.enter_context(patch('app.cabinet.routes.auth.get_client_ip', return_value='1.2.3.4'))
        # First (IP) check trips.
        stack.enter_context(
            patch(
                'app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited',
                AsyncMock(return_value=True),
            )
        )
        stack.enter_context(patch('app.cabinet.routes.auth.verify_and_apply_email_change', verify_apply))
        with pytest.raises(HTTPException) as exc:
            await verify_email_change(
                request=EmailChangeVerifyRequest(code='123456'),
                raw_request=MagicMock(),
                user=_user(),
                db=_db(),
            )
    assert exc.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    verify_apply.assert_not_awaited()  # the code was never even compared


@pytest.mark.asyncio
async def test_verify_per_account_cap_burns_pending_change() -> None:
    verify_apply = AsyncMock(return_value=(True, 'ok'))
    clear_pending = AsyncMock(return_value=None)
    with ExitStack() as stack:
        stack.enter_context(patch('app.cabinet.routes.auth.get_client_ip', return_value='1.2.3.4'))
        # IP check passes, per-account check trips.
        stack.enter_context(
            patch(
                'app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited',
                AsyncMock(side_effect=[False, True]),
            )
        )
        stack.enter_context(patch('app.cabinet.routes.auth.verify_and_apply_email_change', verify_apply))
        stack.enter_context(patch('app.cabinet.routes.auth.clear_email_change_pending', clear_pending))
        with pytest.raises(HTTPException) as exc:
            await verify_email_change(
                request=EmailChangeVerifyRequest(code='123456'),
                raw_request=MagicMock(),
                user=_user(),
                db=_db(),
            )
    assert exc.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    clear_pending.assert_awaited_once()  # pending change burned -> attacker must restart
    verify_apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_change_rejects_unowned_admin_email() -> None:
    with ExitStack() as stack:
        stack.enter_context(patch('app.cabinet.routes.auth.get_client_ip', return_value='1.2.3.4'))
        stack.enter_context(
            patch(
                'app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited',
                AsyncMock(return_value=False),
            )
        )
        # pydantic Settings methods can't be patched on the instance — patch the class.
        stack.enter_context(patch.object(type(settings), 'get_admin_emails', lambda self: ['admin@corp.com']))
        with pytest.raises(HTTPException) as exc:
            await request_email_change(
                request=EmailChangeRequest(new_email='admin@corp.com'),
                raw_request=MagicMock(),
                user=_user(),  # owner@example.com, does NOT own admin@corp.com
                db=_db(),
            )
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_verify_and_apply_rejects_wrong_code_and_applies_correct() -> None:
    pending = SimpleNamespace(
        id=1,
        email='old@example.com',
        email_change_new='new@example.com',
        email_change_code='654321',
        email_change_expires=datetime.now(UTC) + timedelta(minutes=10),
    )
    # Wrong code -> rejected (constant-time compare), pending untouched.
    ok, _ = await verify_and_apply_email_change(_db(), pending, '111111')
    assert ok is False
    assert pending.email_change_code == '654321'

    # Correct code -> applied (email available).
    with patch('app.database.crud.user.get_user_by_email', AsyncMock(return_value=None)):
        ok, _ = await verify_and_apply_email_change(_db(), pending, '654321')
    assert ok is True
    assert pending.email == 'new@example.com'
    assert pending.email_verified is True
