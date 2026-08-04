from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.auth.oauth_providers import OAuthUserInfo
from app.cabinet.routes.account_linking import _exchange_and_link_oauth, link_provider_init
from app.cabinet.routes.oauth import get_oauth_authorize_url, oauth_callback
from app.database.models import UserStatus


def _native_request() -> MagicMock:
    request = MagicMock()
    request.code = None
    request.state = 'csrf-state'
    request.device_id = None
    request.id_token = 'client-id-token'
    request.user = None
    request.campaign_slug = None
    request.referral_code = None
    return request


def _active_user(user_id: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=None,
        username=None,
        first_name='Alice',
        last_name='Google',
        email='alice@example.com',
        email_verified=True,
        status=UserStatus.ACTIVE.value,
        balance_kopeks=0,
        referral_code='REF',
    )


@pytest.mark.asyncio
async def test_google_authorize_ios_stores_client_type_without_web_url() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {}
    provider.get_authorization_url.return_value = 'https://accounts.google.com/o/oauth2/v2/auth?...'

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.generate_oauth_state', AsyncMock(return_value='csrf-state')) as generate_state,
    ):
        response = await get_oauth_authorize_url(provider='google', client_type='ios')

    generate_state.assert_awaited_once()
    assert generate_state.await_args.kwargs['extra_data']['client_type'] == 'ios'
    provider.get_authorization_url.assert_not_called()
    assert response.authorize_url is None
    assert response.state == 'csrf-state'
    assert response.nonce is not None
    assert response.client_type == 'ios'


@pytest.mark.asyncio
async def test_google_authorize_web_returns_provider_url() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {}
    provider.get_authorization_url.return_value = 'https://accounts.google.com/o/oauth2/v2/auth?...'

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.generate_oauth_state', AsyncMock(return_value='csrf-state')),
    ):
        response = await get_oauth_authorize_url(provider='google', client_type='web')

    provider.get_authorization_url.assert_called_once_with('csrf-state')
    assert response.authorize_url == 'https://accounts.google.com/o/oauth2/v2/auth?...'
    assert response.client_type is None


@pytest.mark.asyncio
async def test_google_link_init_android_stores_client_type_without_web_url() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {}
    provider.get_authorization_url.return_value = 'https://accounts.google.com/o/oauth2/v2/auth?...'
    user = SimpleNamespace(id=1, google_id=None)

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.account_linking.generate_oauth_state',
            AsyncMock(return_value='csrf-state'),
        ) as generate_state,
    ):
        response = await link_provider_init(provider='google', client_type='android', user=user)

    generate_state.assert_awaited_once()
    assert generate_state.await_args.kwargs['extra_data']['client_type'] == 'android'
    provider.get_authorization_url.assert_not_called()
    assert response.authorize_url is None
    assert response.state == 'csrf-state'
    assert response.nonce is not None
    assert response.client_type == 'android'


@pytest.mark.asyncio
async def test_google_authorize_native_requires_platform_client_id() -> None:
    provider = MagicMock()
    provider.ensure_client_type_configured.side_effect = ValueError('Google iOS client ID is not configured')

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        pytest.raises(HTTPException) as exc,
    ):
        await get_oauth_authorize_url(provider='google', client_type='ios')

    assert exc.value.status_code == 400
    assert exc.value.detail == 'Google iOS client ID is not configured'


@pytest.mark.asyncio
async def test_google_native_oauth_callback_uses_id_token_without_code() -> None:
    db = AsyncMock()
    created_user = _active_user()
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'client-id-token', '_google_client_type': 'ios'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(
            provider='google',
            provider_id='google-sub',
            email='alice@example.com',
            email_verified=True,
            first_name='Alice',
            last_name='Google',
        )
    )

    with (
        patch(
            'app.cabinet.routes.oauth.validate_oauth_state',
            AsyncMock(return_value={'provider': 'google', 'client_type': 'ios', 'nonce': 'backend-nonce'}),
        ),
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.get_user_by_oauth_provider', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.get_user_by_email', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.get_user_by_referral_code', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.create_user_by_oauth', AsyncMock(return_value=created_user)) as create_user,
        patch('app.cabinet.routes.oauth._finalize_oauth_login', AsyncMock(return_value=MagicMock(name='AuthResponse'))),
    ):
        await oauth_callback(provider='google', request=_native_request(), db=db)

    provider.exchange_code.assert_awaited_once()
    code, kwargs = provider.exchange_code.await_args.args[0], provider.exchange_code.await_args.kwargs
    assert code == ''
    assert kwargs['client_type'] == 'ios'
    assert kwargs['nonce'] == 'backend-nonce'
    assert kwargs['id_token'] == 'client-id-token'

    create_user.assert_awaited_once()
    assert create_user.await_args.kwargs['provider'] == 'google'
    assert create_user.await_args.kwargs['provider_id'] == 'google-sub'
    assert create_user.await_args.kwargs['email_verified'] is True


@pytest.mark.asyncio
async def test_google_native_account_linking_conflict_returns_merge_token() -> None:
    db = AsyncMock()
    current_user = SimpleNamespace(id=1, google_id=None)
    existing_user = SimpleNamespace(id=2)
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'client-id-token', '_google_client_type': 'android'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='google', provider_id='google-sub', email='alice@example.com')
    )

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch('app.cabinet.routes.account_linking.get_user_by_oauth_provider', AsyncMock(return_value=existing_user)),
        patch('app.cabinet.routes.account_linking.create_merge_token', AsyncMock(return_value='merge-token')),
    ):
        result = await _exchange_and_link_oauth(
            db=db,
            user=current_user,
            provider='google',
            code=None,
            state='csrf-state',
            state_data={'provider': 'google', 'client_type': 'android', 'nonce': 'backend-nonce'},
            device_id=None,
            user_payload=None,
            id_token='client-id-token',
            log_context='test',
        )

    assert result.success is False
    assert result.merge_required is True
    assert result.merge_token == 'merge-token'
    kwargs = provider.exchange_code.await_args.kwargs
    assert kwargs['client_type'] == 'android'
    assert kwargs['nonce'] == 'backend-nonce'
    assert kwargs['id_token'] == 'client-id-token'
