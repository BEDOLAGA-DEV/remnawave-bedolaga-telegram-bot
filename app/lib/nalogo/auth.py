"""
Authentication provider implementation.
Based on PHP library's Authenticator class.
"""

import contextlib
import json
import logging
import os
import tempfile
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx

from ._http import AuthProvider
from .dto.device import DeviceInfo
from .exceptions import raise_for_status


logger = logging.getLogger(__name__)


def generate_device_id() -> str:
    """Generate device ID similar to PHP's DeviceIdGenerator."""
    return str(uuid.uuid4()).replace('-', '')[:21].lower()


# DeviceInfo is now imported from dto.device


class AuthProviderImpl(AuthProvider):
    """
    Authentication provider implementation.

    Provides methods for:
    - Username/password authentication (INN + password)
    - Phone-based authentication (2-step: challenge + verify)
    - Token refresh
    - Token storage (in-memory or file-based)
    """

    def __init__(
        self,
        base_url: str = 'https://lknpd.nalog.ru/api',
        storage_path: str | None = None,
        device_id: str | None = None,
        proxy_url: str | None = None,
        timeout: float = 10.0,
    ):
        self.base_url_v1 = f'{base_url}/v1'
        self.base_url_v2 = f'{base_url}/v2'
        self.storage_path = storage_path
        self.device_id = device_id or generate_device_id()
        self.device_info = DeviceInfo(sourceDeviceId=self.device_id)
        self._token_data: dict[str, Any] | None = None
        self.proxy_url = proxy_url
        self.timeout = timeout

        # Default headers similar to PHP Authenticator
        self.default_headers = {
            'Referrer': 'https://lknpd.nalog.ru/auth/login',
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        }

        # Load token from storage if available
        if self.storage_path:
            self._load_token_from_storage()

    def _load_token_from_storage(self) -> None:
        """Load token from file storage."""
        if not self.storage_path:
            return
        storage_path = Path(self.storage_path)
        if not storage_path.exists():
            return

        try:
            with storage_path.open(encoding='utf-8') as f:
                self._token_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Ignore errors, token will be None
            pass

    def _save_token_to_storage(self) -> None:
        """Best-effort atomic persistence; create private temporary files from the start."""
        if not self.storage_path or not self._token_data:
            return
        storage_path = Path(self.storage_path)
        temporary = None
        try:
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                dir=storage_path.parent,
                prefix=f'.{storage_path.name}.',
                suffix='.tmp',
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self._token_data, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, storage_path)
        except OSError as error:
            logger.warning('Failed to persist NaloGO token file: %s', error)
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def reload_token_from_storage(self) -> None:
        """Pick up token files written after this client was constructed."""
        self._load_token_from_storage()

    async def get_token(self) -> dict[str, Any] | None:
        """Get current access token data."""
        return self._token_data

    async def set_token(self, token_json: str) -> None:
        """
        Set access token from JSON string.

        Args:
            token_json: JSON string containing token data
        """
        try:
            self._token_data = json.loads(token_json)
            self._save_token_to_storage()
        except json.JSONDecodeError as e:
            raise ValueError(f'Invalid token JSON: {e}') from e

    async def create_new_access_token(self, username: str, password: str) -> str:
        """
        Create new access token using INN and password.

        Mirrors PHP Authenticator::createAccessToken().

        Args:
            username: INN (tax identification number)
            password: Password

        Returns:
            JSON string with token data

        Raises:
            Domain exceptions for authentication errors
        """
        request_data = {
            'username': username,
            'password': password,
            'deviceInfo': self.device_info.model_dump(),
        }

        async with httpx.AsyncClient(proxy=self.proxy_url) as client:
            response = await client.post(
                f'{self.base_url_v1}/auth/lkfl',
                json=request_data,
                headers=self.default_headers,
                timeout=self.timeout,
            )

            raise_for_status(response)

            # Store and return token
            token_json = response.text
            await self.set_token(token_json)
            return token_json

    async def create_phone_challenge(self, phone: str) -> dict[str, Any]:
        """
        Start phone-based authentication challenge.

        Mirrors PHP ApiClient::createPhoneChallenge() - uses v2 API.

        Args:
            phone: Phone number (e.g., "79000000000")

        Returns:
            Dictionary with challengeToken, expireDate, expireIn

        Raises:
            Domain exceptions for API errors
        """
        request_data = {
            'phone': phone,
            'requireTpToBeActive': True,
        }

        async with httpx.AsyncClient(proxy=self.proxy_url) as client:
            response = await client.post(
                f'{self.base_url_v2}/auth/challenge/sms/start',
                json=request_data,
                headers=self.default_headers,
                timeout=self.timeout,
            )

            raise_for_status(response)
            return response.json()  # type: ignore[no-any-return]

    async def create_new_access_token_by_phone(self, phone: str, challenge_token: str, verification_code: str) -> str:
        """
        Complete phone-based authentication with SMS code.

        Mirrors PHP Authenticator::createAccessTokenByPhone().

        Args:
            phone: Phone number
            challenge_token: Token from create_phone_challenge()
            verification_code: SMS verification code

        Returns:
            JSON string with token data

        Raises:
            Domain exceptions for authentication errors
        """
        request_data = {
            'phone': phone,
            'code': verification_code,
            'challengeToken': challenge_token,
            'deviceInfo': self.device_info.model_dump(),
        }

        async with httpx.AsyncClient(proxy=self.proxy_url) as client:
            response = await client.post(
                f'{self.base_url_v1}/auth/challenge/sms/verify',
                json=request_data,
                headers=self.default_headers,
                timeout=self.timeout,
            )

            raise_for_status(response)

            # Store and return token
            token_json = response.text
            await self.set_token(token_json)
            return token_json

    async def refresh(self, refresh_token: str) -> dict[str, Any] | None:
        """
        Refresh access token using refresh token.

        Mirrors PHP Authenticator::refreshAccessToken().

        Args:
            refresh_token: Refresh token string

        Returns:
            New token data dictionary or None if refresh failed
        """
        request_data = {
            'deviceInfo': self.device_info.model_dump(),
            'refreshToken': refresh_token,
        }

        try:
            async with httpx.AsyncClient(proxy=self.proxy_url) as client:
                response = await client.post(
                    f'{self.base_url_v1}/auth/token',
                    json=request_data,
                    headers=self.default_headers,
                    timeout=self.timeout,
                )

                # PHP version only checks for 200 status
                if response.status_code != HTTPStatus.OK:
                    return None

                # Store and return new token data
                token_json = response.text
                await self.set_token(token_json)
                return self._token_data

        except Exception:
            # Silently fail refresh attempts like PHP version
            return None
