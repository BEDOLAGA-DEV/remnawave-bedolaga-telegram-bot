"""Справочник GEO-РФ (`GET /v1/geo/catalog`): сети, округа, регионы, провайдеры, города.

Бесплатная ручка, но города — тысячи строк: справочные списки кэшируются на десять
минут, города запрашиваются по фильтру или поиску (сервис отдаёт до 500 и говорит,
сколько всего) либо целиком с явным потолком. Округ в query обязан быть латиницей.

Русские имена регионов и городов сервис отдаёт только в строках городов справочника
(`region_ru`, `city_ru`, `district`); в строках прогона — одни токены. Поэтому индекс
имён строится из полного списка городов, а списки регионов и провайдеров, если сервис
прислал их без имён, выводятся из него же.
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

    async def names_index(self, network: str = 'res') -> dict:
        """Имена регионов и городов из полного списка городов (один запрос на десять минут)."""
        catalog = await self.get(network=network, cities_limit=MAX_CITIES_LIMIT)
        return names_from_catalog(catalog)

    def invalidate(self) -> None:
        self._cached.clear()


REGION_TOKEN_KEYS = ('token', 'region', 'code', 'id')
REGION_NAME_KEYS = ('name', 'region_ru', 'name_ru', 'title')


def _first(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ''


def city_name_key(region: str, city: str) -> str:
    return f'{region}|{city}'


def names_from_catalog(catalog: dict) -> dict:
    """{'regions': token → {name, district}, 'cities': 'region|city' → city_ru} из ответа справочника.

    Регионы берутся из `regions[]`, если у них есть имя (ключи терпимы к переименованию),
    и дополняются из строк городов — там имена есть всегда.
    """
    regions: dict[str, dict] = {}
    for item in catalog.get('regions') or []:
        if not isinstance(item, dict):
            continue
        token, name = _first(item, REGION_TOKEN_KEYS), _first(item, REGION_NAME_KEYS)
        if token and name:
            regions[token] = {'name': name, 'district': str(item.get('district') or '')}
    cities: dict[str, str] = {}
    for city in catalog.get('cities') or []:
        if not isinstance(city, dict):
            continue
        region, token = str(city.get('region') or ''), str(city.get('city') or '')
        if region and city.get('region_ru') and region not in regions:
            regions[region] = {'name': str(city['region_ru']), 'district': str(city.get('district') or '')}
        if region and token and city.get('city_ru'):
            cities[city_name_key(region, token)] = str(city['city_ru'])
    return {'regions': regions, 'cities': cities}
