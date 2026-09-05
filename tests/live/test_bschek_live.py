"""Живой детектор дрейфа контракта bschekbot. Только бесплатные ручки, денег не тратит.

Запуск: ``BSCHEK_LIVE_API_KEY=bsk_live_… uv run pytest -m bschek_live tests/live -q``.
Без ключа в окружении — пропускается целиком. Ожидания списаны с записанных
фикстур ``tests/fixtures/bschek`` (см. README там): если тест краснеет, изменился
живой API, и фикстуры с разбором ответов пора перепроверить.
"""

from __future__ import annotations

import os

import pytest

from app.external.bschek_api import BschekAPI, BschekAPIError
from app.services.reachability.cores import XRAY_CORES
from app.services.reachability.units import UnitsCatalog


ENV_KEY = 'BSCHEK_LIVE_API_KEY'

pytestmark = [
    pytest.mark.bschek_live,
    pytest.mark.skipif(not os.environ.get(ENV_KEY), reason=f'{ENV_KEY} не задан — живой API не трогаем'),
]


@pytest.fixture
def api_key() -> str:
    return os.environ[ENV_KEY]


async def test_operators_shape_and_catalog_parsing(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        payload = await api.get_operators()
    assert {'units', 'n_units', 'n_probeable', 'filters', 'n_total'} <= set(payload)
    unit = payload['units'][0]
    assert {'op_key', 'operator', 'name', 'region', 'region_code', 'dpi', 'channel_state', 'probeable'} <= set(unit)
    assert unit['op_key'].count('|') == 2
    catalog = UnitsCatalog.from_response(payload, fetched_at=0.0)
    assert len(catalog.units) == payload['n_units']
    assert catalog.expand([], 'any').resolved


async def test_openapi_core_versions_match_constant(api_key: str) -> None:
    """Версии ядер Xray живут только в описании параметра ``core`` OpenAPI — сверяем константу с ним."""
    async with BschekAPI(api_key=api_key) as api:
        spec = await api.get_openapi()
    text = str(spec)
    for name, version in XRAY_CORES.items():
        assert f"'{name}' = {version}" in text, f'ядро {name}: в OpenAPI больше нет версии {version}'


async def test_account_shape_without_secret(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        account = await api.get_account()
    assert {'balance_credits', 'bonus_credits', 'balance_total', 'tier', 'min_interval_sec'} <= set(account)
    assert 'webhook_secret' not in account


async def test_probe_preview_breakdown(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        preview = await api.preview_probe({'target': 'example.com', 'operators': ['mts'], 'probes': {'tcp': True}})
    assert preview['cost_credits'] > 0
    assert {'base', 'sni_addon', 'multi_scan_factor', 'pre_discount', 'discount_pct', 'total'} <= set(
        preview['breakdown']
    )
    assert isinstance(preview['selected_units'], list)


async def test_probe_preview_sni_needs_both_fields(api_key: str) -> None:
    body = {
        'target': 'example.com',
        'operators': ['mts'],
        'probes': {'tcp': True, 'sni': True},
        'sni_hosts': ['example.com'],
    }
    async with BschekAPI(api_key=api_key) as api:
        preview = await api.preview_probe(body)
    assert preview['breakdown']['sni_addon'] > 0 and preview['legs_with_sni'] >= 1


@pytest.mark.parametrize(
    ('body', 'code'),
    [
        ({'target': 'example.com', 'operators': ['ufo1:mts']}, 'unknown_operator'),
        ({'target': 'example.com', 'operators': ['mts'], 'probes': {'icmp': False, 'tcp': False}}, 'no_probes'),
        ({'operators': ['mts']}, 'invalid_request'),
        ({'targets': [f'h{i}.example' for i in range(11)], 'operators': ['mts']}, 'too_many_targets'),
    ],
)
async def test_validation_codes_still_the_same(api_key: str, body: dict, code: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        with pytest.raises(BschekAPIError) as exc:
            await api.preview_probe(body)
    assert exc.value.code == code


async def test_scan_preview_rejects_non_24(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        with pytest.raises(BschekAPIError) as exc:
            await api.preview_scan({'cidr': '192.0.2.0/25', 'operators': ['mts']})
    assert exc.value.code == 'cidr_not_24'


async def test_scan_preview_shape(api_key: str) -> None:
    async with BschekAPI(api_key=api_key) as api:
        preview = await api.preview_scan({'cidr': '192.0.2.0/24', 'operators': ['mts'], 'probes': {'tcp': True}})
    assert preview['cost_credits'] > 0 and preview['prefix_len'] == 24
    assert {'base', 'total', 'discount_pct'} <= set(preview['breakdown'])


async def test_bad_key_is_unauthenticated() -> None:
    async with BschekAPI(api_key='bsk_live_invalid') as api:
        with pytest.raises(BschekAPIError) as exc:
            await api.get_account()
    assert (exc.value.status, exc.value.code) == (401, 'unauthenticated')
