"""Email account merge now requires an emailed one-time code + an initiator-bound
execute step (account-takeover prevention).

- POST /cabinet/auth/email/register on a conflict mails a code to the EXISTING
  account and returns merge_verification='email_code' (NO merge token yet).
- POST /cabinet/auth/email/merge/verify checks the code, then mints the token.
- POST /cabinet/auth/merge/{token} only runs for the authenticated initiator.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.account_linking import MergeRequest, execute_merge_endpoint
from app.cabinet.routes.auth import register_email, verify_email_merge
from app.cabinet.schemas.auth import EmailMergeVerifyRequest, EmailRegisterRequest
from app.database.models import UserStatus


def _db_returning(existing: object) -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock(return_value=None)
    return db


def _attacker() -> SimpleNamespace:
    return SimpleNamespace(id=1, email=None, email_verified=False, language='en', first_name='Eve')


def _victim() -> SimpleNamespace:
    return SimpleNamespace(id=2, email='victim@example.com', email_verified=True, status=UserStatus.ACTIVE.value)


def _auth(name: str, value: object):
    return patch(f'app.cabinet.routes.auth.{name}', value)


@pytest.mark.asyncio
async def test_email_conflict_sends_code_not_token() -> None:
    """Knowing the victim's email mails a code to THEM — no merge token is issued."""
    store = AsyncMock()
    mint = AsyncMock(return_value='SHOULD-NOT-BE-ISSUED')
    with ExitStack() as s:
        s.enter_context(_auth('get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(_auth('RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
        s.enter_context(_auth('disposable_email_service.is_disposable', MagicMock(return_value=False)))
        s.enter_context(_auth('email_service.is_configured', MagicMock(return_value=True)))
        s.enter_context(_auth('email_service.send_email_change_code', MagicMock()))
        s.enter_context(_auth('get_rendered_override', AsyncMock(return_value=None)))
        s.enter_context(_auth('store_email_merge_otp', store))
        s.enter_context(_auth('create_merge_token', mint))
        result = await register_email(
            request=EmailRegisterRequest(email='victim@example.com', password='whatever-pw'),
            raw_request=MagicMock(),
            user=_attacker(),
            db=_db_returning(_victim()),
        )
    assert result['merge_required'] is True
    assert result['merge_verification'] == 'email_code'
    assert result['merge_token'] is None
    store.assert_awaited_once()
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_wrong_code_rejected() -> None:
    mint = AsyncMock(return_value='tok')
    pending = {'secondary_user_id': 2, 'email': 'victim@example.com', 'code': '654321'}
    with ExitStack() as s:
        s.enter_context(_auth('get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(_auth('RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
        s.enter_context(_auth('get_email_merge_otp', AsyncMock(return_value=pending)))
        s.enter_context(_auth('clear_email_merge_otp', AsyncMock()))
        s.enter_context(_auth('create_merge_token', mint))
        with pytest.raises(HTTPException) as exc:
            await verify_email_merge(
                request=EmailMergeVerifyRequest(code='111111'),
                raw_request=MagicMock(),
                user=_attacker(),
                db=AsyncMock(),
            )
    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_correct_code_issues_token() -> None:
    mint = AsyncMock(return_value='merge-tok-123')
    pending = {'secondary_user_id': 2, 'email': 'victim@example.com', 'code': '654321'}
    with ExitStack() as s:
        s.enter_context(_auth('get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(_auth('RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
        s.enter_context(_auth('get_email_merge_otp', AsyncMock(return_value=pending)))
        s.enter_context(_auth('clear_email_merge_otp', AsyncMock()))
        s.enter_context(_auth('get_user_by_id', AsyncMock(return_value=_victim())))
        s.enter_context(_auth('create_merge_token', mint))
        result = await verify_email_merge(
            request=EmailMergeVerifyRequest(code='654321'),
            raw_request=MagicMock(),
            user=_attacker(),
            db=AsyncMock(),
        )
    assert result['merge_token'] == 'merge-tok-123'
    mint.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_rejects_non_initiator() -> None:
    """A leaked token can't be executed by anyone but the authenticated initiator."""
    consumed = {'primary_user_id': 1, 'secondary_user_id': 2, 'provider': 'email', 'provider_id': 'x'}
    restore = AsyncMock()
    with ExitStack() as s:
        s.enter_context(patch('app.cabinet.routes.account_linking.get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(
            patch('app.cabinet.routes.account_linking.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False))
        )
        s.enter_context(
            patch('app.cabinet.routes.account_linking.consume_merge_token', AsyncMock(return_value=consumed))
        )
        s.enter_context(patch('app.cabinet.routes.account_linking.restore_merge_token', restore))
        with pytest.raises(HTTPException) as exc:
            await execute_merge_endpoint(
                request=MergeRequest(keep_subscription_from=1),
                raw_request=MagicMock(),
                merge_token='x' * 40,
                user=SimpleNamespace(id=99),  # NOT the initiator (primary=1)
                db=AsyncMock(),
            )
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    restore.assert_awaited_once()
