import httpx
import structlog
from app.config import settings

logger = structlog.get_logger(__name__)


class SteamTopUpError(Exception):
    pass


def _format_network_error(exc: Exception) -> str:
    err_msg = str(exc).strip()
    exc_type = type(exc).__name__
    if err_msg:
        return f"{exc_type}: {err_msg}"
    return exc_type


class SteamTopUpClient:
    def __init__(self, base_url: str | None = None):
        self._custom_base_url = base_url

    @property
    def base_url(self) -> str:
        url = self._custom_base_url or getattr(settings, 'STEAM_TOP_UP_API_URL', None) or 'https://tg.slig.app/'
        return url.rstrip('/')

    async def create_stars_order(
        self,
        username: str,
        stars_amount: int,
        source: str | None = None,
        customer_ip: str | None = None,
    ) -> dict:
        """
        Calls Steam Top Up API to create a Stars order.
        Returns dict with 'payment_url' and 'order_id'.
        """
        source_val = source or getattr(settings, 'STEAM_TOP_UP_SOURCE', 'finess')
        clean_username = username.lstrip('@')
        url = f"{self.base_url}/api/telegram/order"
        payload = {
            "product": "stars",
            "username": clean_username,
            "stars_amount": stars_amount,
            "source": source_val,
        }
        if customer_ip:
            payload["customer_ip"] = customer_ip

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(url, json=payload)
                if response.status_code != 200:
                    logger.error(
                        "SteamTopUp API error",
                        status=response.status_code,
                        body=response.text,
                    )
                    raise SteamTopUpError(
                        f"API Error {response.status_code}: {response.text}"
                    )
                return response.json()
            except httpx.RequestError as exc:
                err_detail = _format_network_error(exc)
                logger.error("SteamTopUp network error", error=err_detail, url=url)
                raise SteamTopUpError(f"Network error connecting to SteamTopUp: {err_detail}")

    async def check_order_status(self, order_id: str) -> dict:
        """
        Calls Steam Top Up API to check order status.
        """
        url = f"{self.base_url}/api/telegram/order/{order_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    raise SteamTopUpError(
                        f"API Error {response.status_code}: {response.text}"
                    )
                return response.json()
            except httpx.RequestError as exc:
                err_detail = _format_network_error(exc)
                raise SteamTopUpError(f"Network error connecting to SteamTopUp: {err_detail}")


steam_top_up_client = SteamTopUpClient()

