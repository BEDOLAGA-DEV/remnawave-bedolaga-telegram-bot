"""Справочник GEO: бесплатно, кэшируется, округ в query латиницей, города только по фильтру/поиску."""

from __future__ import annotations

import pytest

from app.services.reachability.geo_catalog import GeoCatalogCache, catalog_params, names_from_catalog
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


async def test_invalidate_forgets_everything() -> None:
    fetch = Fetch({(('network', 'res'),): CATALOG})
    cache = GeoCatalogCache(fetch, clock=FakeClock())
    await cache.get(network='res')
    cache.invalidate()
    await cache.get(network='res')
    assert len(fetch.calls) == 2


def test_names_index_is_built_from_cities_when_regions_come_nameless() -> None:
    # Прод 2026-09-11: regions[] у сервиса без token/name, имена есть только в строках городов.
    catalog = {
        'regions': [{'district': 'ЦФО'}, {'region': 'moscow', 'region_ru': 'Москва', 'district': 'ЦФО'}],
        'cities': [
            {
                'region': 'voronezh_oblast',
                'region_ru': 'Воронежская область',
                'district': 'ЦФО',
                'city': 'voronezh',
                'city_ru': 'Воронеж',
            },
            {
                'region': 'voronezh_oblast',
                'region_ru': 'Воронежская область',
                'district': 'ЦФО',
                'city': 'liski',
                'city_ru': 'Лиски',
            },
            {
                'region': 'moscow',
                'region_ru': 'Москва (из города)',
                'district': 'ЦФО',
                'city': 'moscow',
                'city_ru': 'Москва',
            },
            'мусор',
        ],
    }
    names = names_from_catalog(catalog)
    assert names['regions'] == {
        'moscow': {'name': 'Москва', 'district': 'ЦФО'},
        'voronezh_oblast': {'name': 'Воронежская область', 'district': 'ЦФО'},
    }, 'регион из regions[] с именем главнее, безымянный пропускается, недостающий — из городов'
    assert names['cities'] == {
        'voronezh_oblast|voronezh': 'Воронеж',
        'voronezh_oblast|liski': 'Лиски',
        'moscow|moscow': 'Москва',
    }
    assert names_from_catalog({}) == {'regions': {}, 'cities': {}}


async def test_names_index_asks_for_all_cities_once() -> None:
    fetch = Fetch({(('cities_limit', '5000'), ('network', 'res')): CITIES})
    cache = GeoCatalogCache(fetch, clock=FakeClock())
    first = await cache.names_index()
    await cache.names_index()
    assert first['regions']['voronezh_oblast']['name'] == 'Воронежская область'
    assert first['cities'] == {'voronezh_oblast|voronezh': 'Воронеж'}
    assert len(fetch.calls) == 1
