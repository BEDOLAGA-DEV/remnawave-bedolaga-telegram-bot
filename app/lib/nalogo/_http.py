"""
Internal HTTP client and authentication middleware.
Based on PHP library's AuthenticationPlugin and HTTP architecture.
"""

import asyncio
from abc import ABC, abstractmethod
from http import HTTPStatus
from typing import Any

import httpx

from .exceptions import raise_for_status


class AuthProvider(ABC):
    """Abstract interface for authentication provider."""

    @abstractmethod
    async def get_token(self) -> dict[str, Any] | None:
        """Get current access token data."""

    @abstractmethod
    async def refresh(self, refresh_token: str) -> dict[str, Any] | None:
        """Refresh access token using refresh token."""


class AsyncHTTPClient:
    """
    Async HTTP client with automatic token refresh on 401 responses.

    Based on PHP's AuthenticationPlugin behavior:
    - Adds Bearer authorization header
    - On 401 response, attempts token refresh once
    - Retries request with new token (max 2 attempts)
    """

    def __init__(
        self,
        base_url: str,
        auth_provider: AuthProvider,
        default_headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        proxy_url: str | None = None,
    ):
        self.base_url = base_url
        self.auth_provider = auth_provider
        self.default_headers = default_headers or {}
        self.timeout = timeout
        self.proxy_url = proxy_url
        self._refresh_lock = asyncio.Lock()
        self.max_retries = 2  # Same as PHP AuthenticationPlugin::RETRY_LIMIT

    async def _get_auth_headers(self) -> dict[str, str]:
        """Get authorization headers from current token."""
        token_data = await self.auth_provider.get_token()
        if not token_data or 'token' not in token_data:
            return {}

        return {'Authorization': f'Bearer {token_data["token"]}'}

    async def _handle_401_response(self, client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response | None:
        """
        Handle 401 response by refreshing token and retrying request.

        Uses asyncio.Lock to prevent concurrent refresh attempts,
        similar to PHP's retry storage mechanism.
        """
        async with self._refresh_lock:
            token_data = await self.auth_provider.get_token()
            if not token_data or 'refreshToken' not in token_data:
                return None

            # Attempt token refresh
            new_token_data = await self.auth_provider.refresh(token_data['refreshToken'])
            if not new_token_data or 'token' not in new_token_data:
                return None

            # Update request with new authorization header
            new_auth_headers = {'Authorization': f'Bearer {new_token_data["token"]}'}
            request.headers.update(new_auth_headers)

            # Retry request with new token
            return await client.send(request)

    async def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        Make HTTP request with automatic auth and 401 retry logic.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., "/income")
            headers: Additional headers
            json_data: JSON request body
            **kwargs: Additional httpx.AsyncClient.request arguments

        Returns:
            httpx.Response object

        Raises:
            Domain exceptions via raise_for_status()
        """
        # Prepare headers
        request_headers = self.default_headers.copy()
        auth_headers = await self._get_auth_headers()
        request_headers.update(auth_headers)
        if headers:
            request_headers.update(headers)

        # Prepare request parameters
        request_kwargs = {
            'method': method,
            'url': self.base_url + path,
            'headers': request_headers,
            'timeout': self.timeout,
            **kwargs,
        }

        if json_data is not None:
            request_kwargs['json'] = json_data

        async with httpx.AsyncClient(proxy=self.proxy_url) as client:
            # Initial request
            response = await client.request(**request_kwargs)

            # Handle 401 with token refresh (max 1 retry)
            if response.status_code == HTTPStatus.UNAUTHORIZED:
                # Build request object for retry
                request = client.build_request(**request_kwargs)
                retry_response = await self._handle_401_response(client, request)
                if retry_response is not None:
                    response = retry_response

            # Check for domain exceptions
            raise_for_status(response)

            return response

    async def get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """GET request."""
        return await self.request('GET', path, headers=headers, **kwargs)

    async def post(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """POST request with JSON data."""
        return await self.request('POST', path, headers=headers, json_data=json_data, **kwargs)

    async def put(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """PUT request with JSON data."""
        return await self.request('PUT', path, headers=headers, json_data=json_data, **kwargs)

    async def delete(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """DELETE request."""
        return await self.request('DELETE', path, headers=headers, **kwargs)
