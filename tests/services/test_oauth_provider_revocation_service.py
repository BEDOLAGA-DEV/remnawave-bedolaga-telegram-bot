from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.cabinet.auth.oauth_providers import OAuthUserInfo
from app.services.oauth_provider_revocation_service import OAuthProviderRevocationService


@pytest.mark.asyncio
async def test_revoke_google_authorization_code_revokes_refresh_token() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(
        return_value={
            'access_token': 'google-access-token',
            'refresh_token': 'google-refresh-token',
        }
    )
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='google', provider_id='google-sub', email='user@example.com')
    )
    response = SimpleNamespace(status_code=200, text='')

    with patch('app.services.oauth_provider_revocation_service.httpx.AsyncClient') as client_class:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client

        result = await OAuthProviderRevocationService().revoke_authorization_code(
            provider_name='google',
            oauth_provider=provider,
            code='auth-code',
            expected_provider_id='google-sub',
            client_type='web',
        )

    provider.exchange_code.assert_awaited_once_with('auth-code', client_type='web')
    provider.get_user_info.assert_awaited_once_with(
        {'access_token': 'google-access-token', 'refresh_token': 'google-refresh-token'}
    )
    client.post.assert_awaited_once()
    assert client.post.await_args.args[0] == 'https://oauth2.googleapis.com/revoke'
    assert client.post.await_args.kwargs['data']['token'] == 'google-refresh-token'
    assert client.post.await_args.kwargs['data']['token_type_hint'] == 'refresh_token'
    assert client.post.await_args.kwargs['headers'] == {'Content-Type': 'application/x-www-form-urlencoded'}
    assert result.provider == 'google'
    assert result.provider_id == 'google-sub'
    assert result.token_type == 'refresh_token'


@pytest.mark.asyncio
async def test_revoke_google_authorization_code_falls_back_to_access_token() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'access_token': 'google-access-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='google', provider_id='google-sub', email='user@example.com')
    )
    response = SimpleNamespace(status_code=200, text='')

    with patch('app.services.oauth_provider_revocation_service.httpx.AsyncClient') as client_class:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client

        result = await OAuthProviderRevocationService().revoke_authorization_code(
            provider_name='google',
            oauth_provider=provider,
            code='auth-code',
            expected_provider_id='google-sub',
            client_type='web',
        )

    assert client.post.await_args.kwargs['data']['token'] == 'google-access-token'
    assert client.post.await_args.kwargs['data']['token_type_hint'] == 'access_token'
    assert result.token_type == 'access_token'


@pytest.mark.asyncio
async def test_revoke_apple_authorization_code_uses_client_secret_and_refresh_token() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(
        return_value={
            'id_token': 'apple-id-token',
            'access_token': 'apple-access-token',
            'refresh_token': 'apple-refresh-token',
        }
    )
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='apple', provider_id='apple-sub', email='user@example.com')
    )
    provider._client_id_for.return_value = 'com.bitnet.ios'
    provider.create_client_secret.return_value = 'apple-client-secret'
    response = SimpleNamespace(status_code=200, text='')

    with patch('app.services.oauth_provider_revocation_service.httpx.AsyncClient') as client_class:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client

        result = await OAuthProviderRevocationService().revoke_authorization_code(
            provider_name='apple',
            oauth_provider=provider,
            code='auth-code',
            expected_provider_id='apple-sub',
            client_type='ios',
        )

    provider.exchange_code.assert_awaited_once_with('auth-code', client_type='ios')
    provider._client_id_for.assert_called_once_with('ios')
    provider.create_client_secret.assert_called_once_with(client_type='ios')
    client.post.assert_awaited_once()
    assert client.post.await_args.args[0] == 'https://appleid.apple.com/auth/revoke'
    assert client.post.await_args.kwargs['data']['client_id'] == 'com.bitnet.ios'
    assert client.post.await_args.kwargs['data']['client_secret'] == 'apple-client-secret'
    assert client.post.await_args.kwargs['data']['token'] == 'apple-refresh-token'
    assert client.post.await_args.kwargs['data']['token_type_hint'] == 'refresh_token'
    assert client.post.await_args.kwargs['headers'] == {'Content-Type': 'application/x-www-form-urlencoded'}
    assert result.provider == 'apple'
    assert result.provider_id == 'apple-sub'
    assert result.token_type == 'refresh_token'


@pytest.mark.asyncio
async def test_revoke_apple_authorization_code_uses_web_client_id_for_web_flow() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'access_token': 'apple-access-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='apple', provider_id='apple-sub', email='user@example.com')
    )
    provider._client_id_for.return_value = 'com.bitnet.web'
    provider.create_client_secret.return_value = 'apple-web-client-secret'
    response = SimpleNamespace(status_code=200, text='')

    with patch('app.services.oauth_provider_revocation_service.httpx.AsyncClient') as client_class:
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client

        await OAuthProviderRevocationService().revoke_authorization_code(
            provider_name='apple',
            oauth_provider=provider,
            code='auth-code',
            expected_provider_id='apple-sub',
            client_type='web',
        )

    provider._client_id_for.assert_called_once_with('web')
    provider.create_client_secret.assert_called_once_with(client_type='web')
    assert client.post.await_args.kwargs['data']['client_id'] == 'com.bitnet.web'
    assert client.post.await_args.kwargs['data']['client_secret'] == 'apple-web-client-secret'
    assert client.post.await_args.kwargs['data']['token'] == 'apple-access-token'
    assert client.post.await_args.kwargs['data']['token_type_hint'] == 'access_token'


@pytest.mark.asyncio
async def test_revoke_authorization_code_rejects_mismatched_provider_identity() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'access_token': 'token'})
    provider.get_user_info = AsyncMock(return_value=OAuthUserInfo(provider='google', provider_id='other-sub'))

    with pytest.raises(ValueError, match='does not match current account'):
        await OAuthProviderRevocationService().revoke_authorization_code(
            provider_name='google',
            oauth_provider=provider,
            code='auth-code',
            expected_provider_id='google-sub',
            client_type='web',
        )


@pytest.mark.asyncio
async def test_revoke_authorization_code_requires_revocable_token() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'id_token': 'id-token'})
    provider.get_user_info = AsyncMock(return_value=OAuthUserInfo(provider='apple', provider_id='apple-sub'))

    with pytest.raises(ValueError, match='missing access_token or refresh_token'):
        await OAuthProviderRevocationService().revoke_authorization_code(
            provider_name='apple',
            oauth_provider=provider,
            code='auth-code',
            expected_provider_id='apple-sub',
            client_type='ios',
        )


@pytest.mark.asyncio
async def test_revoke_authorization_code_maps_exchange_failures_to_value_error() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(side_effect=RuntimeError('provider unavailable'))
    provider.get_user_info = AsyncMock()

    with pytest.raises(ValueError, match='Failed to exchange authorization code for provider revocation'):
        await OAuthProviderRevocationService().revoke_authorization_code(
            provider_name='google',
            oauth_provider=provider,
            code='auth-code',
            expected_provider_id='google-sub',
            client_type='web',
        )

    provider.get_user_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_google_authorization_code_maps_network_failure_to_value_error() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'access_token': 'google-access-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='google', provider_id='google-sub', email='user@example.com')
    )

    with patch('app.services.oauth_provider_revocation_service.httpx.AsyncClient') as client_class:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError('network down'))
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client

        with pytest.raises(ValueError, match='google token revocation failed'):
            await OAuthProviderRevocationService().revoke_authorization_code(
                provider_name='google',
                oauth_provider=provider,
                code='auth-code',
                expected_provider_id='google-sub',
                client_type='web',
            )


@pytest.mark.asyncio
async def test_revoke_apple_authorization_code_maps_network_failure_to_value_error() -> None:
    provider = MagicMock()
    provider.exchange_code = AsyncMock(return_value={'refresh_token': 'apple-refresh-token'})
    provider.get_user_info = AsyncMock(
        return_value=OAuthUserInfo(provider='apple', provider_id='apple-sub', email='user@example.com')
    )
    provider._client_id_for.return_value = 'com.bitnet.ios'
    provider.create_client_secret.return_value = 'apple-client-secret'

    with patch('app.services.oauth_provider_revocation_service.httpx.AsyncClient') as client_class:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.ReadTimeout('timeout'))
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client_class.return_value = client

        with pytest.raises(ValueError, match='apple token revocation failed'):
            await OAuthProviderRevocationService().revoke_authorization_code(
                provider_name='apple',
                oauth_provider=provider,
                code='auth-code',
                expected_provider_id='apple-sub',
                client_type='ios',
            )
