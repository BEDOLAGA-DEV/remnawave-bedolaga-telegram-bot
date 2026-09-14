"""Сторож: каждая ручка /v1/geo, которую зовёт клиент, есть в схеме bschekbot GEO.

Схема снята 2026-09-11 (`X-API-Updated: 2026-09-11`) с https://bsbord.com/v1/geo/openapi.json.
Обновление сервиса: положить новый файл в fixtures и прогнать тест.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / 'fixtures' / 'bschek_geo_openapi_2026-09-11.json'
CLIENT = ROOT / 'app' / 'external' / 'bschek_api.py'

_REQUEST = re.compile(r"""_request\(\s*['"](?P<method>GET|POST|DELETE)['"]\s*,\s*f?['"](?P<path>/geo/[^'"]+)['"]""")


def _normalize(method: str, path: str) -> str:
    return f'{method.upper()} /v1{re.sub(r"\{[^}]*\}", "{}", path)}'


def _spec_endpoints() -> set[str]:
    spec = json.loads(FIXTURE.read_text(encoding='utf-8'))
    found: set[str] = set()
    for path, methods in spec['paths'].items():
        for method in methods:
            found.add(_normalize(method, path.removeprefix('/v1')))
    return found


def _called_endpoints() -> set[str]:
    text = CLIENT.read_text(encoding='utf-8')
    return {_normalize(m.group('method'), m.group('path')) for m in _REQUEST.finditer(text)}


def test_every_geo_call_of_the_client_exists_in_the_spec() -> None:
    called = _called_endpoints()
    assert called, 'клиент обязан звать хотя бы одну ручку /v1/geo'
    missing = called - _spec_endpoints()
    assert not missing, f'ручек нет в схеме GEO: {sorted(missing)}'


def test_client_covers_the_five_endpoints_we_use() -> None:
    expected = {
        'GET /v1/geo/catalog',
        'POST /v1/geo/preview',
        'POST /v1/geo/runs',
        'GET /v1/geo/runs/{}',
        'POST /v1/geo/runs/{}/cancel',
    }
    assert expected <= _called_endpoints()
