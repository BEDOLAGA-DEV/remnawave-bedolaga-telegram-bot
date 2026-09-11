"""Справочник GEO-РФ (`GET /v1/geo/catalog`): сети, округа, регионы, провайдеры, города.

Бесплатная ручка, но города — тысячи строк: справочные списки кэшируются на десять
минут, города запрашиваются только по фильтру или поиску (сервис отдаёт до 500
и говорит, сколько всего). Округ в query обязан быть латиницей.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from app.services.reachability.geo_requests import normalize_district


DEFAULT_TTL = 600.0
MAX_CITIES_LIMIT = 5000


def catalog_params(
    *,
    network: str = 'res',
    q: str | None = None,
    isp: str | None = None,
    region: str | None = None,
    district: str | None = None,
    cities_limit: int | None = None,
) -> dict[str, str]:
    """Query к сервису: пустые фильтры не уходят, округ — латиницей, потолок городов — в рамках 1..5000."""
    params: dict[str, str] = {'network': network}
    if q:
        params['city'] = q.strip()
    if isp:
        params['isp'] = isp
    if region:
        params['region'] = region
    if district:
        params['district'] = normalize_district(district)
    if cities_limit:
        params['cities_limit'] = str(min(max(int(cities_limit), 1), MAX_CITIES_LIMIT))
    return params


class GeoCatalogCache:
    def __init__(
        self,
        fetch: Callable[[dict[str, str]], Awaitable[dict]],
        ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._cached: dict[tuple, tuple[float, dict]] = {}

    async def get(self, **filters) -> dict:
        params = catalog_params(**filters)
        key = tuple(sorted(params.items()))
        now = self._clock()
        hit = self._cached.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        data = await self._fetch(params)
        self._cached[key] = (now, data)
        return data

    async def regions_index(self, network: str = 'res') -> dict[str, dict]:
        """token → {name, district} — чтобы подписать строки прогона без похода в каталог за каждой."""
        catalog = await self.get(network=network)
        return {
            str(item.get('token')): {'name': str(item.get('name') or ''), 'district': str(item.get('district') or '')}
            for item in catalog.get('regions') or []
            if isinstance(item, dict) and item.get('token')
        }

    def invalidate(self) -> None:
        self._cached.clear()
