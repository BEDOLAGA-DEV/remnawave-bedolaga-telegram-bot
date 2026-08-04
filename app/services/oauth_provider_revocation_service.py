from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
import structlog

from app.cabinet.auth.oauth_providers import OAuthProvider, OAuthTokenResponse

logger = structlog.get_logger(__name__)

OAuthRevocationProvider = Literal['google', 'apple']
OAuthRevocationPurpose = Literal['unlink', 'delete']
OAuthRevocationStatus = Literal['succeeded', 'failed']

_GOOGLE_REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
_APPLE_REVOKE_URL = 'https://appleid.apple.com/auth/revoke'


@dataclass(frozen=True)
class OAuthProviderRevocationResult:
    provider: OAuthRevocationProvider
    provider_id: str
    token_type: Literal['access_token', 'refresh_token']


class OAuthProviderRevocationService:
    async def revoke_authorization_code(
        self,
        *,
        provider_name: OAuthRevocationProvider,
        oauth_provider: OAuthProvider,
        code: str,
        expected_provider_id: str,
        client_type: str,
        exchange_kwargs: dict[str, Any] | None = None,
    ) -> OAuthProviderRevocationResult:
        """Exchange a fresh provider code, verify account ownership, and revoke its token immediately."""
        if not code:
            raise ValueError('Authorization code is required for provider revocation')
        try:
            exchange_params = dict(exchange_kwargs or {})
            exchange_params.pop('client_type', None)
            token_data = await oauth_provider.exchange_code(
                code,
                client_type=client_type,
                **exchange_params,
            )
        except Exception as exc:
            raise ValueError('Failed to exchange authorization code for provider revocation') from exc

        try:
            user_info = await oauth_provider.get_user_info(token_data)
        except Exception as exc:
            raise ValueError('Failed to fetch provider identity for revocation') from exc

        if user_info.provider_id != expected_provider_id:
            raise ValueError(f'{provider_name} authorization does not match current account')

        token, token_type = self._select_revocable_token(token_data)
        if provider_name == 'google':
            await self._revoke_google_token(token, token_type)
        elif provider_name == 'apple':
            await self._revoke_apple_token(oauth_provider, token, token_type, client_type)
        else:
            raise ValueError(f'Unsupported provider revocation: {provider_name}')

        logger.info(
            'OAuth provider token revoked',
            provider=provider_name,
            provider_id=expected_provider_id,
            token_type=token_type,
        )
        return OAuthProviderRevocationResult(
            provider=provider_name,
            provider_id=expected_provider_id,
            token_type=token_type,
        )

    @staticmethod
    def _select_revocable_token(token_data: OAuthTokenResponse) -> tuple[str, Literal['access_token', 'refresh_token']]:
        refresh_token = token_data.get('refresh_token')
        if refresh_token:
            return refresh_token, 'refresh_token'
        access_token = token_data.get('access_token')
        if access_token:
            return access_token, 'access_token'
        raise ValueError('Provider token response missing access_token or refresh_token')

    async def _revoke_google_token(self, token: str, token_type: Literal['access_token', 'refresh_token']) -> None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    _GOOGLE_REVOKE_URL,
                    data={
                        'token': token,
                        'token_type_hint': token_type,
                    },
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                )
        except httpx.HTTPError as exc:
            raise ValueError('google token revocation failed') from exc
        self._raise_for_revocation_error('google', response)

    async def _revoke_apple_token(
        self,
        oauth_provider: OAuthProvider,
        token: str,
        token_type: Literal['access_token', 'refresh_token'],
        client_type: str,
    ) -> None:
        client_id_for = getattr(oauth_provider, '_client_id_for', None)
        create_client_secret = getattr(oauth_provider, 'create_client_secret', None)
        if not callable(client_id_for) or not callable(create_client_secret):
            raise ValueError('Apple provider revocation is not configured')

        client_id = client_id_for(client_type)
        client_secret = create_client_secret(client_type=client_type)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    _APPLE_REVOKE_URL,
                    data={
                        'client_id': client_id,
                        'client_secret': client_secret,
                        'token': token,
                        'token_type_hint': token_type,
                    },
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                )
        except httpx.HTTPError as exc:
            raise ValueError('apple token revocation failed') from exc
        self._raise_for_revocation_error('apple', response)

    @staticmethod
    def _raise_for_revocation_error(provider: str, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        raise ValueError(f'{provider} token revocation failed ({response.status_code}): {response.text}')


oauth_provider_revocation_service = OAuthProviderRevocationService()
