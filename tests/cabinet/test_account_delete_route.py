from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.auth import delete_current_account
from app.cabinet.schemas.auth import AccountDeleteRequest


@pytest.mark.asyncio
async def test_delete_current_account_accepts_current_password() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.verify_password', return_value=True),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='current-password'),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_delete_current_account_rejects_wrong_password() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.verify_password', return_value=False),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='wrong-password'),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == 'Invalid current password'
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_accepts_matching_telegram_init_data() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=555)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.validate_telegram_init_data', return_value={'id': 555}),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', telegram_init_data='signed-init-data'),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_delete_current_account_rejects_oauth_only_user_without_fresh_proof() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=None, google_id='google-user')
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE'),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == 'Account ownership cannot be verified'
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_rejects_mismatched_telegram_init_data() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=555)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.validate_telegram_init_data', return_value={'id': 777}),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', telegram_init_data='signed-init-data'),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == 'Invalid Telegram confirmation'
    delete_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_rolls_back_on_service_failure() -> None:
    user = SimpleNamespace(id=123, password_hash='hashed-password', telegram_id=None)
    db = AsyncMock()
    delete_account = AsyncMock(side_effect=RuntimeError('boom'))

    with (
        patch('app.cabinet.routes.auth.verify_password', return_value=True),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', password='current-password'),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert exc.value.detail == 'Failed to delete account'
    db.rollback.assert_awaited_once()
