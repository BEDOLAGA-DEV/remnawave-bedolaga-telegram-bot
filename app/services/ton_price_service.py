"""Сервис получения курса TON/RUB с CoinGecko."""

from __future__ import annotations

import time
from typing import Any

import aiohttp
import structlog


logger = structlog.get_logger(__name__)

_COINGECKO_URL = (
    'https://api.coingecko.com/api/v3/simple/price'
    '?ids=the-open-network&vs_currencies=rub'
)
_CACHE_TTL = 300  # 5 минут
_NANO = 1_000_000_000


class TonPriceService:
    """Получает актуальный курс TON в рублях, кеширует на 5 минут."""

    def __init__(self) -> None:
        self._cached_rate: float | None = None
        self._cached_at: float = 0.0

    async def _fetch_rate(self) -> float | None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(_COINGECKO_URL) as resp:
                    if resp.status != 200:
                        logger.warning('CoinGecko вернул неожиданный статус', status=resp.status)
                        return None
                    data: dict[str, Any] = await resp.json()
                    rate = data.get('the-open-network', {}).get('rub')
                    if rate is None:
                        logger.warning('CoinGecko: поле rub не найдено в ответе', data=data)
                        return None
                    return float(rate)
        except Exception as error:
            logger.warning('Ошибка получения курса TON с CoinGecko', error=error)
            return None

    async def get_rate_rub(self) -> float | None:
        """Возвращает курс 1 TON в рублях (с кешем 5 мин)."""
        now = time.monotonic()
        if self._cached_rate is not None and (now - self._cached_at) < _CACHE_TTL:
            return self._cached_rate

        rate = await self._fetch_rate()
        if rate is not None:
            self._cached_rate = rate
            self._cached_at = now
            logger.info('Курс TON обновлён', rate_rub=rate)
        elif self._cached_rate is not None:
            logger.warning('Используем кешированный курс TON', cached_rate=self._cached_rate)

        return self._cached_rate

    async def rub_to_nano(self, rub_amount: float) -> int | None:
        """Конвертирует рубли в нанотоны. Возвращает None если курс недоступен."""
        rate = await self.get_rate_rub()
        if rate is None or rate <= 0:
            return None
        ton_amount = rub_amount / rate
        return int(ton_amount * _NANO)
