"""Справочник GEO: бесплатно, кэшируется, округ в query латиницей, города только по фильтру/поиску."""

from __future__ import annotations

import pytest

from app.services.reachability.geo_catalog import GeoCatalogCache, catalog_params
from tests.services.reachability.fakes import FakeClock


pytestmark = pytest.mark.asyncio

CATALOG = {
    'networks': ['res', 'mob'],
    'districts': [{'code': 'cfo', 'name': 'ЦФО'}],
    'regions': [
        {'token': 'moscow', 'name': 'Москва', 'district': 'ЦФО'},
        {'token': 'voronezh_oblast', 'name': 'Воронежская область', 'district': 'ЦФО'},
    ],
    'isps': [{'token': 'mts', 'name': 'МТС', 'cities': 43}],
    'cities_hint': 'задайте фильтр или cities_limit',
}
CITIES = {
    **CATALOG,
    'cities': [
        {
            'region': 'voronezh_oblast',
            'region_ru': 'Воронежская область',
            'district': 'ЦФО',
            'city': 'voronezh',
            'city_ru': 'Воронеж',
            'isps': ['rostelecom'],
        }
    ],
    'cities_total': 1,
    'cities_truncated': False,
}


class Fetch:
    def __init__(self, answers: dict[tuple, dict]) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    async def __call__(self, params: dict[str, str]) -> dict:
        self.calls.append(dict(params))
        return self.answers[tuple(sorted(params.items()))]


def test_catalog_params_use_latin_districts_and_drop_empty_values() -> None:
    assert catalog_params(network='res', district='ЦФО', q='Воронеж', cities_limit=None) == {
        'network': 'res',
        'district': 'cfo',
        'city': 'Воронеж',
    }


def test_catalog_params_clamp_cities_limit_to_the_service_range() -> None:
    assert catalog_params(cities_limit=99_999)['cities_limit'] == '5000'
    assert catalog_params(cities_limit=0) == {'network': 'res'}


async def test_reference_lists_are_cached_for_ten_minutes() -> None:
    clock = FakeClock()
    fetch = Fetch({(('network', 'res'),): CATALOG})
    cache = GeoCatalogCache(fetch, clock=clock)
    first = await cache.get(network='res')
    second = await cache.get(network='res')
    assert first is second and len(fetch.calls) == 1
    clock.now += 601
    await cache.get(network='res')
    assert len(fetch.calls) == 2


async def test_city_search_is_its_own_cache_key() -> None:
    fetch = Fetch({(('network', 'res'),): CATALOG, (('city', 'воронеж'), ('network', 'res')): CITIES})
    cache = GeoCatalogCache(fetch, clock=FakeClock())
    found = await cache.get(network='res', q='воронеж')
    assert found['cities'][0]['city'] == 'voronezh' and found['cities_total'] == 1
    assert len(fetch.calls) == 1


async def test_regions_index_maps_token_to_name_and_district() -> None:
    fetch = Fetch({(('network', 'res'),): CATALOG})
    cache = GeoCatalogCache(fetch, clock=FakeClock())
    index = await cache.regions_index()
    assert index['voronezh_oblast'] == {'name': 'Воронежская область', 'district': 'ЦФО'}


async def test_invalidate_forgets_everything() -> None:
    fetch = Fetch({(('network', 'res'),): CATALOG})
    cache = GeoCatalogCache(fetch, clock=FakeClock())
    await cache.get(network='res')
    cache.invalidate()
    await cache.get(network='res')
    assert len(fetch.calls) == 2
