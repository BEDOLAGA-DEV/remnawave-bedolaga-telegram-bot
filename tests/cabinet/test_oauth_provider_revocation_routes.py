from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.auth.oauth_providers import OAuthUserInfo
from app.cabinet.routes.account_linking import (
    RevokeProviderCallbackRequest,
    revoke_provider_callback,
    revoke_provider_init,
    unlink_provider,
)
from app.cabinet.routes.auth import delete_current_account
from app.cabinet.schemas.auth import AccountDeleteRequest
from app.services.oauth_provider_revocation_service import OAuthProviderRevocationResult


def _make_raw_request(ip: str = '127.0.0.1') -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip), headers={})


@pytest.mark.asyncio
async def test_revoke_provider_init_returns_authorize_url_and_state() -> None:
    user = SimpleNamespace(id=123, google_id='google-sub')
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {}
    provider.get_authorization_url.return_value = 'https://accounts.google.com/auth?state=state-1'

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch('app.cabinet.routes.account_linking.generate_oauth_state', AsyncMock(return_value='state-1')) as state_mock,
    ):
        response = await revoke_provider_init('google', purpose='delete', client_type='ios', user=user)

    state_mock.assert_awaited_once()
    assert state_mock.await_args.args[0] == 'google'
    assert state_mock.await_args.kwargs['extra_data']['revoking'] == 'true'
    assert state_mock.await_args.kwargs['extra_data']['purpose'] == 'delete'
    assert state_mock.await_args.kwargs['extra_data']['user_id'] == '123'
    assert state_mock.await_args.kwargs['extra_data']['client_type'] == 'ios'
    provider.get_authorization_url.assert_called_once_with('state-1')
    assert response.authorize_url == 'https://accounts.google.com/auth?state=state-1'
    assert response.state == 'state-1'
    assert response.client_type == 'ios'


@pytest.mark.asyncio
async def test_revoke_provider_callback_returns_delete_proof_for_matching_provider() -> None:
    user = SimpleNamespace(id=123, google_id='google-sub')
    db = AsyncMock()
    db.add = MagicMock()
    provider = MagicMock()
    revocation_result = OAuthProviderRevocationResult(
        provider='google',
        provider_id='google-sub',
        token_type='refresh_token',
    )

    with (
        patch(
            'app.cabinet.routes.account_linking.validate_oauth_state',
            AsyncMock(return_value={'revoking': 'true', 'purpose': 'delete', 'user_id': '123', 'client_type': 'web'}),
        ),
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.account_linking.oauth_provider_revocation_service.revoke_authorization_code',
            AsyncMock(return_value=revocation_result),
        ),
        patch(
            'app.cabinet.routes.account_linking.create_oauth_revocation_proof',
            AsyncMock(return_value='proof-token'),
        ),
    ):
        response = await revoke_provider_callback(
            'google',
            RevokeProviderCallbackRequest(code='auth-code', state='state-1'),
            user=user,
            db=db,
        )

    assert response.success is True
    assert response.provider == 'google'
    assert response.proof_token == 'proof-token'
    assert response.message == 'revoked'


@pytest.mark.asyncio
async def test_revoke_provider_callback_uses_real_service_without_duplicate_client_type() -> None:
    user = SimpleNamespace(id=123, google_id='google-sub')
    db = AsyncMock()
    db.add = MagicMock()
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'access_token': 'google-access-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='google', provider_id='google-sub', email='user@example.com')
    )
    response = SimpleNamespace(status_code=200, text='')

    with (
        patch(
            'app.cabinet.routes.account_linking.validate_oauth_state',
            AsyncMock(
                return_value={
                    'revoking': 'true',
                    'purpose': 'delete',
                    'user_id': '123',
                    'client_type': 'ios',
                    'nonce': 'nonce-1',
                }
            ),
        ),
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch('app.cabinet.routes.account_linking.create_oauth_revocation_proof', AsyncMock(return_value='proof-token')),
        patch('app.services.oauth_provider_revocation_service.httpx.AsyncClient') as client_class,
    ):
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client

        result = await revoke_provider_callback(
            'google',
            RevokeProviderCallbackRequest(code='auth-code', state='state-1'),
            user=user,
            db=db,
        )

    assert result.success is True
    assert result.proof_token == 'proof-token'
    provider.exchange_code.assert_awaited_once_with(
        'auth-code',
        client_type='ios',
        state='state-1',
        nonce='nonce-1',
    )


@pytest.mark.asyncio
async def test_revoke_provider_callback_records_failed_exchange_as_bad_request() -> None:
    user = SimpleNamespace(id=123, google_id='google-sub')
    db = AsyncMock()
    db.add = MagicMock()
    provider = MagicMock()

    with (
        patch(
            'app.cabinet.routes.account_linking.validate_oauth_state',
            AsyncMock(return_value={'revoking': 'true', 'purpose': 'delete', 'user_id': '123', 'client_type': 'web'}),
        ),
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.account_linking.create_oauth_revocation_proof',
            AsyncMock(return_value='proof-token'),
        ),
        patch(
            'app.cabinet.routes.account_linking.consume_oauth_revocation_proof',
            AsyncMock(),
        ),
        patch(
            'app.cabinet.routes.account_linking.oauth_provider_revocation_service.revoke_authorization_code',
            AsyncMock(side_effect=ValueError('Failed to exchange authorization code for provider revocation')),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await revoke_provider_callback(
            'google',
            RevokeProviderCallbackRequest(code='auth-code', state='state-1'),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == 'Failed to exchange authorization code for provider revocation'
    db.add.assert_called_once()
    failed_event = db.add.call_args.args[0]
    assert failed_event.status == 'failed'
    assert failed_event.error_message == 'Failed to exchange authorization code for provider revocation'
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_revoke_provider_callback_does_not_revoke_when_delete_proof_storage_fails() -> None:
    user = SimpleNamespace(id=123, google_id='google-sub')
    db = AsyncMock()
    db.add = MagicMock()
    provider = MagicMock()
    revoke = AsyncMock()

    with (
        patch(
            'app.cabinet.routes.account_linking.validate_oauth_state',
            AsyncMock(return_value={'revoking': 'true', 'purpose': 'delete', 'user_id': '123', 'client_type': 'web'}),
        ),
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.account_linking.create_oauth_revocation_proof',
            AsyncMock(side_effect=RuntimeError('redis down')),
        ),
        patch('app.cabinet.routes.account_linking.oauth_provider_revocation_service.revoke_authorization_code', revoke),
        pytest.raises(HTTPException) as exc,
    ):
        await revoke_provider_callback(
            'google',
            RevokeProviderCallbackRequest(code='auth-code', state='state-1'),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert exc.value.detail == 'Failed to prepare OAuth revocation proof'
    revoke.assert_not_awaited()
    db.add.assert_called_once()
    failed_event = db.add.call_args.args[0]
    assert failed_event.status == 'failed'
    assert failed_event.error_message == 'Failed to store OAuth revocation proof'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_provider_callback_rejects_last_auth_method_unlink() -> None:
    user = SimpleNamespace(
        id=123,
        google_id='google-sub',
        apple_id=None,
        yandex_id=None,
        discord_id=None,
        vk_id=None,
        password_hash=None,
        telegram_id=None,
        email=None,
    )
    db = AsyncMock()
    provider = MagicMock()
    revoke = AsyncMock()

    with (
        patch(
            'app.cabinet.routes.account_linking.validate_oauth_state',
            AsyncMock(return_value={'revoking': 'true', 'purpose': 'unlink', 'user_id': '123', 'client_type': 'web'}),
        ),
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch('app.cabinet.routes.account_linking.oauth_provider_revocation_service.revoke_authorization_code', revoke),
        pytest.raises(HTTPException) as exc,
    ):
        await revoke_provider_callback(
            'google',
            RevokeProviderCallbackRequest(code='auth-code', state='state-1'),
            user=user,
            db=db,
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == 'Cannot unlink last authentication method'
    revoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_google_unlink_requires_revocation_flow() -> None:
    user = SimpleNamespace(id=123, google_id='google-sub')
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await unlink_provider('google', user=user, db=db)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == 'Provider revocation is required before unlinking this provider'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_current_account_accepts_matching_oauth_revocation_proof() -> None:
    user = SimpleNamespace(id=123, password_hash=None, telegram_id=None, google_id='google-sub', apple_id=None)
    db = AsyncMock()
    delete_account = AsyncMock()

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        patch('app.cabinet.routes.auth.RateLimitCache.is_rate_limited', AsyncMock(return_value=False)),
        patch(
            'app.cabinet.routes.auth.get_oauth_revocation_proof',
            AsyncMock(
                return_value={
                    'user_id': 123,
                    'provider': 'google',
                    'provider_id': 'google-sub',
                    'purpose': 'delete',
                    'event_id': 77,
                }
            ),
        ),
        patch('app.cabinet.routes.auth.account_deletion_service.delete_own_account', delete_account),
    ):
        response = await delete_current_account(
            AccountDeleteRequest(confirmation='DELETE', oauth_revocation_proofs=['proof-token-abcdefghijklmnopqrstuvwxyz123456']),
            raw_request=_make_raw_request(),
            user=user,
            db=db,
        )

    assert response.message == 'Account deletion requested'
    delete_account.assert_awaited_once_with(db, user, oauth_revocation_event_ids=[77])
