"""
Receipt API implementation.
Based on PHP library's Api\\Receipt class.
"""

from typing import Any

from ._http import AsyncHTTPClient


class ReceiptAPI:
    """
    Receipt API for accessing receipt information.

    Provides async methods for:
    - Getting receipt print URL
    - Getting receipt JSON data

    Maps to PHP Api\\Receipt functionality.
    """

    def __init__(self, http_client: AsyncHTTPClient, base_endpoint: str, user_inn: str):
        self.http = http_client
        self.base_endpoint = base_endpoint
        self.user_inn = user_inn

    def print_url(self, receipt_uuid: str) -> str:
        """
        Compose receipt print URL.

        Maps to PHP Receipt::printUrl() method.
        This method composes the URL without making HTTP request.

        Args:
            receipt_uuid: Receipt UUID

        Returns:
            Complete URL for receipt printing

        Raises:
            ValueError: If receipt_uuid is empty
        """
        if not receipt_uuid.strip():
            raise ValueError('Receipt UUID cannot be empty')

        # Public print URLs on lknpd.nalog.ru are served under /api/v1/receipt/...
        # rather than /api/receipt/..., so normalize to the versioned base endpoint.
        path = f'/receipt/{self.user_inn}/{receipt_uuid.strip()}/print'
        base_endpoint = self.base_endpoint.rstrip('/')
        if not base_endpoint.endswith('/v1'):
            base_endpoint = f'{base_endpoint}/v1'
        return f'{base_endpoint}{path}'

    async def json(self, receipt_uuid: str) -> dict[str, Any]:
        """
        Get receipt data in JSON format.

        Maps to PHP Receipt::json() method.

        Args:
            receipt_uuid: Receipt UUID

        Returns:
            Dictionary with receipt JSON data

        Raises:
            ValueError: If receipt_uuid is empty
            DomainException: For API errors
        """
        if not receipt_uuid.strip():
            raise ValueError('Receipt UUID cannot be empty')

        # Make GET request like PHP: sprintf('/receipt/%s/%s/json', $this->profile->getInn(), $receiptUuid)
        path = f'/receipt/{self.user_inn}/{receipt_uuid.strip()}/json'
        response = await self.http.get(path)

        return response.json()  # type: ignore[no-any-return]
