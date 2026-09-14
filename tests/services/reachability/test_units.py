"""Каталог симок: раскрытие селекторов по живому списку и расчёт пропусков.

Preview API не отдаёт skipped_*, а голый оператор в probe не даёт skipped_dpi_off,
поэтому «что заказано, но не пошло» считаем сами. Неизвестный оператор у API — 503,
у нас — ошибка валидации ДО траты денег.
"""

from __future__ import annotations

import pytest

from app.services.reachability.units import Selector, SelectorError, UnitsCache, UnitsCatalog, parse_selector
from tests.fixtures.bschek_fixtures import load_bschek_fixture


@pytest.fixture
def catalog() -> UnitsCatalog:
    return UnitsCatalog.from_response(load_bschek_fixture('operators')['body'], fetched_at=0.0)


def test_catalog_from_response(catalog: UnitsCatalog) -> None:
    assert len(catalog.units) == 30
    unit = catalog.by_key['mts|цфо|off']
    assert (unit.operator, unit.region, unit.region_code, unit.dpi, unit.probeable) == (
        'mts',
        'ЦФО',
        'cfo',
        'off',
        True,
    )


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('mts', Selector('mts', None, None)),
        ('mts|цфо|off', Selector('mts', 'цфо', 'off')),
        ('mts|*|on', Selector('mts', None, 'on')),
        ('mts||on', Selector('mts', None, 'on')),
        ('*|цфо|on', Selector(None, 'цфо', 'on')),
        ('|цфо|on', Selector(None, 'цфо', 'on')),
        ('MTS|CFO|OFF', Selector('mts', 'cfo', 'off')),
        ('*|*|off', Selector(None, None, 'off')),
    ],
)
def test_parse_selector(raw: str, expected: Selector) -> None:
    assert parse_selector(raw) == expected


@pytest.mark.parametrize('raw', ['', '*|*|*', 'mts|цфо|off|extra', 'ufo1:mts', 'mts|цфо|maybe'])
def test_parse_selector_rejects(raw: str) -> None:
    with pytest.raises(SelectorError):
        parse_selector(raw)


def test_expand_bare_operator_with_dpi_on_skips_off_units(catalog: UnitsCatalog) -> None:
    result = catalog.expand(['mts'], dpi='on')
    assert result.resolved == ['mts|пфо|on']
    assert sorted(u.op_key for u in result.skipped_dpi_off) == ['mts|дфо|off', 'mts|цфо|off']
    assert result.unknown == []


def test_expand_region_selector_and_latin_code(catalog: UnitsCatalog) -> None:
    assert catalog.expand(['*|цфо|on'], dpi='on').resolved == [
        'megafon|цфо|on',
        'tele2|цфо|on',
        't-mobile|цфо|on',
        'dobro|цфо|on',
        'sberm|цфо|on',
    ]
    assert catalog.expand(['mts|cfo|off'], dpi='off').resolved == ['mts|цфо|off']


def test_expand_any_keeps_both_groups_and_dedups(catalog: UnitsCatalog) -> None:
    result = catalog.expand(['mts', 'mts|цфо|off'], dpi='any')
    assert result.resolved == ['mts|цфо|off', 'mts|дфо|off', 'mts|пфо|on']


def test_expand_empty_means_whole_fleet_by_dpi(catalog: UnitsCatalog) -> None:
    assert len(catalog.expand([], dpi='on').resolved) == 15
    assert len(catalog.expand([], dpi='any').resolved) == 30


def test_expand_reports_unknown_selectors_instead_of_dropping_them(catalog: UnitsCatalog) -> None:
    result = catalog.expand(['nokia|цфо|on', 'mts|пфо|on'], dpi='on')
    assert result.unknown == ['nokia|цфо|on']
    assert result.resolved == ['mts|пфо|on']


def test_expand_marks_non_probeable_units_unavailable() -> None:
    payload = load_bschek_fixture('operators')['body']
    payload['units'][0]['probeable'] = False
    catalog = UnitsCatalog.from_response(payload, fetched_at=0.0)
    key = payload['units'][0]['op_key']
    result = catalog.expand([key], dpi='any')
    assert result.resolved == []
    assert [u.op_key for u in result.skipped_unavailable] == [key]


def test_expand_rejects_unknown_dpi_mode(catalog: UnitsCatalog) -> None:
    with pytest.raises(SelectorError):
        catalog.expand(['mts'], dpi='maybe')


async def test_cache_refetches_after_ttl_and_on_force() -> None:
    calls = 0
    now = [0.0]

    async def fetch() -> dict:
        nonlocal calls
        calls += 1
        return load_bschek_fixture('operators')['body']

    cache = UnitsCache(fetch, ttl=60.0, clock=lambda: now[0])
    await cache.get()
    await cache.get()
    assert calls == 1
    now[0] = 61.0
    await cache.get()
    assert calls == 2
    await cache.get(force=True)
    assert calls == 3
