from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.auth.oauth_providers import OAuthUserInfo
from app.cabinet.routes.account_linking import _exchange_and_link_oauth, link_provider_init
from app.cabinet.routes.oauth import get_oauth_authorize_url, oauth_callback
from app.database.models import UserStatus


def _request() -> MagicMock:
    request = MagicMock()
    request.code = 'auth-code'
    request.state = 'csrf-state'
    request.device_id = None
    request.id_token = 'client-id-token'
    request.user = {'name': {'firstName': 'Alice', 'lastName': 'Appleseed'}, 'email': 'alice@example.com'}
    request.campaign_slug = None
    request.referral_code = None
    return request


def _active_user(user_id: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=None,
        username=None,
        first_name='Alice',
        last_name='Appleseed',
        email='alice@example.com',
        email_verified=True,
        status=UserStatus.ACTIVE.value,
        balance_kopeks=0,
        referral_code='REF',
    )


@pytest.mark.asyncio
async def test_apple_authorize_ios_stores_client_type_without_web_url() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {'nonce': 'backend-nonce', '_nonce': 'backend-nonce'}
    provider.get_authorization_url.return_value = 'https://appleid.apple.com/auth/authorize?...'

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.generate_oauth_state', AsyncMock(return_value='csrf-state')) as generate_state,
    ):
        response = await get_oauth_authorize_url(provider='apple', client_type='ios')

    generate_state.assert_awaited_once()
    assert generate_state.await_args.kwargs['extra_data']['client_type'] == 'ios'
    provider.get_authorization_url.assert_not_called()
    assert response.authorize_url is None
    assert response.state == 'csrf-state'
    assert response.nonce == 'backend-nonce'
    assert response.client_type == 'ios'


@pytest.mark.asyncio
async def test_apple_authorize_web_returns_provider_url() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {'nonce': 'backend-nonce', '_nonce': 'backend-nonce'}
    provider.get_authorization_url.return_value = 'https://appleid.apple.com/auth/authorize?...'

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.generate_oauth_state', AsyncMock(return_value='csrf-state')),
    ):
        response = await get_oauth_authorize_url(provider='apple', client_type='web')

    assert provider.get_authorization_url.call_args.kwargs['_client_type'] == 'web'
    assert response.authorize_url == 'https://appleid.apple.com/auth/authorize?...'
    assert response.client_type == 'web'


@pytest.mark.asyncio
async def test_apple_link_init_ios_stores_client_type_without_web_url() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {'nonce': 'backend-nonce', '_nonce': 'backend-nonce'}
    provider.get_authorization_url.return_value = 'https://appleid.apple.com/auth/authorize?...'
    user = SimpleNamespace(id=1, apple_id=None)

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.account_linking.generate_oauth_state',
            AsyncMock(return_value='csrf-state'),
        ) as generate_state,
    ):
        response = await link_provider_init(provider='apple', client_type='ios', user=user)

    generate_state.assert_awaited_once()
    assert generate_state.await_args.kwargs['extra_data']['client_type'] == 'ios'
    provider.get_authorization_url.assert_not_called()
    assert response.authorize_url is None
    assert response.state == 'csrf-state'
    assert response.nonce == 'backend-nonce'
    assert response.client_type == 'ios'


@pytest.mark.asyncio
async def test_apple_oauth_callback_passes_nonce_and_user_payload_to_provider() -> None:
    db = AsyncMock()
    created_user = _active_user()
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'server-id-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(
            provider='apple',
            provider_id='apple-sub',
            email='alice@example.com',
            email_verified=True,
            first_name='Alice',
            last_name='Appleseed',
        )
    )

    with (
        patch(
            'app.cabinet.routes.oauth.validate_oauth_state',
            AsyncMock(return_value={'nonce': 'backend-nonce', 'client_type': 'ios'}),
        ),
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.get_user_by_oauth_provider', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.get_user_by_email', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.get_user_by_referral_code', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.create_user_by_oauth', AsyncMock(return_value=created_user)) as create_user,
        patch('app.cabinet.routes.oauth._finalize_oauth_login', AsyncMock(return_value=MagicMock(name='AuthResponse'))),
    ):
        await oauth_callback(provider='apple', request=_request(), db=db)

    provider.exchange_code.assert_awaited_once()
    _code, kwargs = provider.exchange_code.await_args.args[0], provider.exchange_code.await_args.kwargs
    assert _code == 'auth-code'
    assert kwargs['nonce'] == 'backend-nonce'
    assert kwargs['client_type'] == 'ios'
    assert 'id_token' not in kwargs
    assert kwargs['user']['name']['firstName'] == 'Alice'

    create_user.assert_awaited_once()
    assert create_user.await_args.kwargs['provider'] == 'apple'
    assert create_user.await_args.kwargs['provider_id'] == 'apple-sub'
    assert create_user.await_args.kwargs['email_verified'] is True


@pytest.mark.asyncio
async def test_apple_account_linking_conflict_returns_merge_token() -> None:
    db = AsyncMock()
    current_user = SimpleNamespace(id=1, apple_id=None)
    existing_user = SimpleNamespace(id=2)
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'server-id-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='apple', provider_id='apple-sub', email='alice@example.com')
    )

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch('app.cabinet.routes.account_linking.get_user_by_oauth_provider', AsyncMock(return_value=existing_user)),
        patch('app.cabinet.routes.account_linking.create_merge_token', AsyncMock(return_value='merge-token')),
    ):
        result = await _exchange_and_link_oauth(
            db=db,
            user=current_user,
            provider='apple',
            code='auth-code',
            state='csrf-state',
            state_data={'provider': 'apple', 'nonce': 'backend-nonce', 'client_type': 'ios'},
            device_id=None,
            user_payload={'name': {'firstName': 'Alice'}},
            log_context='test',
        )

    assert result.success is False
    assert result.merge_required is True
    assert result.merge_token == 'merge-token'
    kwargs = provider.exchange_code.await_args.kwargs
    assert kwargs['nonce'] == 'backend-nonce'
    assert kwargs['client_type'] == 'ios'
    assert 'id_token' not in kwargs
    assert kwargs['user']['name']['firstName'] == 'Alice'
