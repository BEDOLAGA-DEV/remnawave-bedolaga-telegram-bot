from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.auth.oauth_providers import OAuthUserInfo
from app.cabinet.routes.account_linking import (
    LinkCallbackRequest,
    RevokeProviderCallbackRequest,
    _exchange_and_link_oauth,
    link_provider_init,
)
from app.cabinet.routes.oauth import OAuthCallbackRequest, get_oauth_authorize_url, oauth_callback
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
    provider.resolve_client_id.return_value = 'com.example.replacement'

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.generate_oauth_state', AsyncMock(return_value='csrf-state')) as generate_state,
    ):
        response = await get_oauth_authorize_url(
            provider='apple', client_type='ios', client_id='com.example.replacement'
        )

    provider.resolve_client_id.assert_called_once_with('ios', 'com.example.replacement')
    generate_state.assert_awaited_once()
    assert generate_state.await_args.kwargs['extra_data']['client_type'] == 'ios'
    assert generate_state.await_args.kwargs['extra_data']['apple_client_id'] == 'com.example.replacement'
    provider.get_authorization_url.assert_not_called()
    assert response.authorize_url is None
    assert response.state == 'csrf-state'
    assert response.nonce == 'backend-nonce'
    assert response.client_type == 'ios'


@pytest.mark.asyncio
async def test_apple_authorize_ios_without_client_id_binds_legacy_default() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {'nonce': 'backend-nonce', '_nonce': 'backend-nonce'}
    provider.resolve_client_id.return_value = 'com.example.legacy'

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.oauth.generate_oauth_state',
            AsyncMock(return_value='csrf-state'),
        ) as generate_state,
    ):
        await get_oauth_authorize_url(provider='apple', client_type='ios')

    provider.resolve_client_id.assert_called_once_with('ios', None)
    assert (
        generate_state.await_args.kwargs['extra_data']['apple_client_id']
        == 'com.example.legacy'
    )


@pytest.mark.asyncio
async def test_apple_authorize_rejects_unknown_native_client_before_state_creation() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {'nonce': 'backend-nonce', '_nonce': 'backend-nonce'}
    provider.resolve_client_id.side_effect = ValueError('Apple iOS client ID is not configured')
    generate_state = AsyncMock()

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.generate_oauth_state', generate_state),
        pytest.raises(HTTPException) as exc,
    ):
        await get_oauth_authorize_url(
            provider='apple', client_type='ios', client_id='com.attacker.app'
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == 'Apple iOS client ID is not configured'
    generate_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_apple_authorize_web_rejects_explicit_client_id_before_state_creation() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {'nonce': 'backend-nonce', '_nonce': 'backend-nonce'}
    generate_state = AsyncMock()

    with (
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.generate_oauth_state', generate_state),
        pytest.raises(HTTPException) as exc,
    ):
        await get_oauth_authorize_url(
            provider='apple',
            client_type='web',
            client_id='com.example.replacement',
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == 'Explicit client_id is supported only for native Apple OAuth'
    generate_state.assert_not_awaited()


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
    provider.resolve_client_id.return_value = 'com.example.replacement'
    user = SimpleNamespace(id=1, apple_id=None)

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.account_linking.generate_oauth_state',
            AsyncMock(return_value='csrf-state'),
        ) as generate_state,
    ):
        response = await link_provider_init(
            provider='apple', client_type='ios', client_id='com.example.replacement', user=user
        )

    generate_state.assert_awaited_once()
    assert generate_state.await_args.kwargs['extra_data']['client_type'] == 'ios'
    assert generate_state.await_args.kwargs['extra_data']['apple_client_id'] == 'com.example.replacement'
    provider.get_authorization_url.assert_not_called()
    assert response.authorize_url is None
    assert response.state == 'csrf-state'
    assert response.nonce == 'backend-nonce'
    assert response.client_type == 'ios'


@pytest.mark.asyncio
async def test_google_link_init_rejects_explicit_apple_client_id_before_state_creation() -> None:
    provider = MagicMock()
    provider.prepare_auth_state.return_value = {}
    user = SimpleNamespace(id=1, google_id=None)
    generate_state = AsyncMock()

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch('app.cabinet.routes.account_linking.generate_oauth_state', generate_state),
        pytest.raises(HTTPException) as exc,
    ):
        await link_provider_init(
            provider='google',
            client_type='ios',
            client_id='com.example.replacement',
            user=user,
        )

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == 'Explicit client_id is supported only for native Apple OAuth'
    generate_state.assert_not_awaited()


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
            AsyncMock(return_value={'nonce': 'backend-nonce', 'client_type': 'ios', 'apple_client_id': 'com.example.replacement'}),
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
    assert kwargs['apple_client_id'] == 'com.example.replacement'
    assert 'id_token' not in kwargs
    assert kwargs['user']['name']['firstName'] == 'Alice'

    create_user.assert_awaited_once()
    assert create_user.await_args.kwargs['provider'] == 'apple'
    assert create_user.await_args.kwargs['provider_id'] == 'apple-sub'
    assert create_user.await_args.kwargs['email_verified'] is True


@pytest.mark.asyncio
async def test_replacement_apple_client_returns_existing_subject_account() -> None:
    db = AsyncMock()
    existing_user = _active_user(user_id=321)
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'server-id-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(
            provider='apple',
            provider_id='existing-apple-sub',
            email=None,
            email_verified=False,
        )
    )
    create_user = AsyncMock()
    finalize = AsyncMock(return_value=MagicMock(name='AuthResponse'))

    with (
        patch(
            'app.cabinet.routes.oauth.validate_oauth_state',
            AsyncMock(
                return_value={
                    'nonce': 'backend-nonce',
                    'client_type': 'ios',
                    'apple_client_id': 'com.example.replacement',
                }
            ),
        ),
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.oauth.get_user_by_oauth_provider',
            AsyncMock(return_value=existing_user),
        ),
        patch('app.cabinet.routes.oauth.create_user_by_oauth', create_user),
        patch('app.cabinet.routes.oauth._finalize_oauth_login', finalize),
    ):
        await oauth_callback(provider='apple', request=_request(), db=db)

    assert provider.exchange_code.await_args.kwargs['apple_client_id'] == 'com.example.replacement'
    create_user.assert_not_awaited()
    assert finalize.await_args.args[1] is existing_user


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
            state_data={'provider': 'apple', 'nonce': 'backend-nonce', 'client_type': 'ios', 'apple_client_id': 'com.example.replacement'},
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
    assert kwargs['apple_client_id'] == 'com.example.replacement'
    assert 'id_token' not in kwargs
    assert kwargs['user']['name']['firstName'] == 'Alice'


@pytest.mark.asyncio
async def test_apple_account_linking_success_forwards_state_bound_client_id() -> None:
    db = AsyncMock()
    current_user = SimpleNamespace(id=1, apple_id=None, email='alice@example.com')
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'server-id-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(
            provider='apple',
            provider_id='apple-sub',
            email=None,
            email_verified=False,
        )
    )
    set_provider_id = AsyncMock()

    with (
        patch('app.cabinet.routes.account_linking.get_provider', return_value=provider),
        patch(
            'app.cabinet.routes.account_linking.get_user_by_oauth_provider',
            AsyncMock(return_value=None),
        ),
        patch(
            'app.cabinet.routes.account_linking.set_user_oauth_provider_id',
            set_provider_id,
        ),
    ):
        result = await _exchange_and_link_oauth(
            db=db,
            user=current_user,
            provider='apple',
            code='auth-code',
            state='csrf-state',
            state_data={
                'provider': 'apple',
                'nonce': 'backend-nonce',
                'client_type': 'ios',
                'apple_client_id': 'com.example.replacement',
            },
            device_id=None,
            user_payload={'name': {'firstName': 'Alice'}},
            log_context='test',
        )

    assert result.success is True
    assert result.message == 'linked'
    assert provider.exchange_code.await_args.kwargs['apple_client_id'] == 'com.example.replacement'
    set_provider_id.assert_awaited_once_with(db, current_user, 'apple', 'apple-sub')
    db.commit.assert_awaited_once()


# --- M8: callback-substitution regression ---


def test_oauth_callback_model_drops_rogue_client_id() -> None:
    """Pydantic extra='ignore' prevents a rogue client_id from reaching exchange_code."""
    request = OAuthCallbackRequest(code='auth-code', state='csrf-state', client_id='com.attacker.app')
    assert not hasattr(request, 'client_id')


def test_link_callback_model_drops_rogue_client_id() -> None:
    request = LinkCallbackRequest(code='auth-code', state='csrf-state', client_id='com.attacker.app')
    assert not hasattr(request, 'client_id')


def test_revoke_callback_model_drops_rogue_client_id() -> None:
    request = RevokeProviderCallbackRequest(code='auth-code', state='csrf-state', client_id='com.attacker.app')
    assert not hasattr(request, 'client_id')


@pytest.mark.asyncio
async def test_oauth_callback_state_wins_over_rogue_client_id() -> None:
    """A rogue client_id in the callback body cannot displace the state-bound apple_client_id."""
    db = AsyncMock()
    created_user = _active_user()
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'server-id-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(
            provider='apple',
            provider_id='apple-sub',
            email=None,
            email_verified=False,
        )
    )
    # Actual model: rogue client_id is silently dropped by Pydantic before it reaches the route.
    request = OAuthCallbackRequest(code='auth-code', state='csrf-state', client_id='com.attacker.app')

    with (
        patch(
            'app.cabinet.routes.oauth.validate_oauth_state',
            AsyncMock(return_value={'nonce': 'backend-nonce', 'client_type': 'ios', 'apple_client_id': 'com.example.replacement'}),
        ),
        patch('app.cabinet.routes.oauth.get_provider', return_value=provider),
        patch('app.cabinet.routes.oauth.get_user_by_oauth_provider', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.get_user_by_email', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.get_user_by_referral_code', AsyncMock(return_value=None)),
        patch('app.cabinet.routes.oauth.create_user_by_oauth', AsyncMock(return_value=created_user)),
        patch('app.cabinet.routes.oauth._finalize_oauth_login', AsyncMock(return_value=MagicMock(name='AuthResponse'))),
    ):
        await oauth_callback(provider='apple', request=request, db=db)

    assert not hasattr(request, 'client_id')
    assert provider.exchange_code.await_args.kwargs['apple_client_id'] == 'com.example.replacement'
