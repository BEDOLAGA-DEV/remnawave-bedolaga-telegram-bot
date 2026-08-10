import httpx
import structlog
from app.config import settings

logger = structlog.get_logger(__name__)


class SteamTopUpError(Exception):
    pass


class SteamTopUpClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.STEAM_TOP_UP_API_URL or 'https://tg.slig.app/').rstrip('/')

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
                logger.error("SteamTopUp network error", error=str(exc))
                raise SteamTopUpError(f"Network error connecting to SteamTopUp: {exc}")

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
                raise SteamTopUpError(f"Network error connecting to SteamTopUp: {exc}")


steam_top_up_client = SteamTopUpClient()
