from datetime import UTC, datetime

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class CurrencyConverter:
    def __init__(self):
        self._cache = {}
        self._cache_ttl = 3600  # 1 час
        self._last_update = {}

    async def get_usd_to_rub_rate(self) -> float:
        """Получает курс USD/RUB с кешированием"""

        cache_key = 'USD_RUB'
        now = datetime.now(UTC)

        # Проверяем кеш
        if (
            cache_key in self._cache
            and cache_key in self._last_update
            and (now - self._last_update[cache_key]).total_seconds() < self._cache_ttl
        ):
            return self._cache[cache_key]

        # Получаем новый курс
        rate = await self._fetch_exchange_rate()

        if rate:
            self._cache[cache_key] = rate
            self._last_update[cache_key] = now
            logger.info('Обновлен курс USD/RUB', rate=rate)
            return rate

        # Возвращаем из кеша если API недоступен
        if cache_key in self._cache:
            logger.warning('API курсов недоступен, используем кешированный курс')
            return self._cache[cache_key]

        fallback_rate = settings.USD_RUB_FALLBACK_RATE
        if fallback_rate is not None and fallback_rate > 0:
            logger.warning('Используем fallback курс USD/RUB из конфигурации', rate=fallback_rate)
            return float(fallback_rate)

        raise RuntimeError('Не удалось получить курс USD/RUB и fallback отключен')

    async def _fetch_exchange_rate(self) -> float | None:
        """Получает курс с нескольких источников"""

        sources = [self._fetch_from_cbr, self._fetch_from_exchangerate_api, self._fetch_from_fixer]

        for source in sources:
            try:
                rate = await source()
                if rate and 50 < rate < 200:  # Разумные границы курса
                    return rate
            except Exception as e:
                logger.debug('Ошибка получения курса из', __name__=source.__name__, error=e)
                continue

        return None

    async def _fetch_from_cbr(self) -> float | None:
        """Получает курс с сайта ЦБ РФ"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get('https://www.cbr-xml-daily.ru/daily_json.js') as response:
                    if response.status == 200:
                        data = await response.json()
                        usd_rate = data['Valute']['USD']['Value']
                        return float(usd_rate)
        except Exception as e:
            logger.debug('Ошибка получения курса ЦБ', error=e)
            return None

    async def _fetch_from_exchangerate_api(self) -> float | None:
        """Получает курс с exchangerate-api.com"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get('https://api.exchangerate-api.com/v4/latest/USD') as response:
                    if response.status == 200:
                        data = await response.json()
                        rub_rate = data['rates']['RUB']
                        return float(rub_rate)
        except Exception as e:
            logger.debug('Ошибка получения курса exchangerate-api', error=e)
            return None

    async def _fetch_from_fixer(self) -> float | None:
        """Получает курс с fixer.io (бесплатный план)"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                # Используем бесплатный endpoint (EUR base)
                async with session.get(
                    'https://api.fixer.io/latest?access_key=YOUR_API_KEY&symbols=USD,RUB'
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('success'):
                            # Конвертируем EUR -> USD -> RUB
                            usd_eur = data['rates']['USD']
                            rub_eur = data['rates']['RUB']
                            usd_rub = rub_eur / usd_eur
                            return float(usd_rub)
        except Exception as e:
            logger.debug('Ошибка получения курса fixer', error=e)
            return None

    async def usd_to_rub(self, usd_amount: float) -> float:
        """Конвертирует USD в RUB"""
        rate = await self.get_usd_to_rub_rate()
        return usd_amount * rate

    async def rub_to_usd(self, rub_amount: float) -> float:
        """Конвертирует RUB в USD"""
        rate = await self.get_usd_to_rub_rate()
        return rub_amount / rate


# Глобальный экземпляр
currency_converter = CurrencyConverter()
