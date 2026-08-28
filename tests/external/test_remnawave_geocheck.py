"""Remnawave 3.3.0 Connections API — GeoCheck.

Панель 3.3.0 добавила асинхронную проверку геоданных ноды:
``POST /api/connections/geocheck/{nodeUuid}`` ставит задачу и отдаёт ``jobId``,
``GET /api/connections/geocheck/{jobId}`` возвращает статус и результат.

Два неочевидных места, которые тут закреплены:

  * ``requestBody`` у POST помечен required. Режим «по умолчанию» — это пустой
    объект ``{}``, а ``_make_request`` исторически отбрасывал falsy-тело
    (``if data:``), то есть запрос уходил бы вообще без JSON. Тест ниже требует,
    чтобы пустое тело всё-таки уходило.
  * ``ips`` из ``NodeResponseDto`` панель отдаёт с 3.х, но парсер ноды его не
    читал. Без него кабинету нечего показать в выборе исходного IP.
"""

from __future__ import annotations

import json
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest
from yarl import URL

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


NODE_UUID = '11111111-1111-1111-1111-111111111111'


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


class _FakeResponse:
    def __init__(self, status: int = 200, body: str = '') -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses = list(responses) or [_FakeResponse(204)]
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({'method': method, **kwargs})
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]

    def query_string(self, index: int = -1) -> str:
        call = self.calls[index]
        return URL(call['url']).with_query(call['params'] or {}).query_string


def _api_with_session(*responses: _FakeResponse) -> tuple[RemnaWaveAPI, _FakeSession]:
    api = _api()
    session = _FakeSession(*responses)
    api.session = session
    return api, session


def _node_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'uuid': NODE_UUID,
        'name': 'Germany | WI-FI | #2',
        'address': '213.176.77.249',
        'countryCode': 'DE',
        'isConnected': True,
        'isDisabled': False,
        'usersOnline': 10,
        'trafficUsedBytes': 0,
        'trafficLimitBytes': 0,
        'versions': {'xray': '26.7.28', 'node': '3.3.0'},
        'ips': [
            {'ip': '213.176.77.249', 'status': 'OUTBOUND'},
            {'ip': '2a0b:4141:820:140d::2', 'status': 'INBOUND'},
        ],
        'system': {'info': {'networkInterfaces': ['lo', 'ens3']}},
    }
    payload.update(overrides)
    return payload


# ============== Постановка задачи ==============


async def test_request_geocheck_posts_to_connections_endpoint():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'jobId': 'job-1'}})

    job_id = await api.request_node_geocheck(NODE_UUID)

    method, endpoint = api._make_request.call_args.args[:2]
    assert (method, endpoint) == ('POST', f'/api/connections/geocheck/{NODE_UUID}')
    assert job_id == 'job-1'


async def test_request_geocheck_default_mode_sends_empty_json_body():
    """requestBody у команды required: «по умолчанию» — это ``{}``, а не отсутствие тела."""
    api, session = _api_with_session(_FakeResponse(201, json.dumps({'response': {'jobId': 'job-1'}})))

    await api.request_node_geocheck(NODE_UUID)

    assert session.last['json'] == {}


async def test_request_geocheck_sends_ip_only():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'jobId': 'job-1'}})

    await api.request_node_geocheck(NODE_UUID, ip='1.2.3.4')

    assert api._make_request.call_args.args[2] == {'ip': '1.2.3.4'}


async def test_request_geocheck_sends_interface_only():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'jobId': 'job-1'}})

    await api.request_node_geocheck(NODE_UUID, interface='ens3')

    assert api._make_request.call_args.args[2] == {'interface': 'ens3'}


async def test_request_geocheck_rejects_ip_and_interface_together():
    """Панель выбирает один источник маршрута; отправлять оба — молча неоднозначно."""
    api = _api()
    api._make_request = AsyncMock()

    with pytest.raises(RemnaWaveAPIError):
        await api.request_node_geocheck(NODE_UUID, ip='1.2.3.4', interface='ens3')

    api._make_request.assert_not_called()


async def test_request_geocheck_ignores_blank_values():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'jobId': 'job-1'}})

    await api.request_node_geocheck(NODE_UUID, ip='   ', interface='')

    assert api._make_request.call_args.args[2] == {}


async def test_request_geocheck_raises_when_panel_returns_no_job_id():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {}})

    with pytest.raises(RemnaWaveAPIError):
        await api.request_node_geocheck(NODE_UUID)


# ============== Опрос результата ==============


async def test_get_geocheck_result_uses_job_id_path():
    api = _api()
    api._make_request = AsyncMock(return_value={'response': {'isCompleted': False, 'isFailed': False, 'result': None}})

    result = await api.get_node_geocheck_result('job-1')

    assert api._make_request.call_args.args[:2] == ('GET', '/api/connections/geocheck/job-1')
    assert result == {'isCompleted': False, 'isFailed': False, 'result': None}


async def test_get_geocheck_result_returns_completed_payload():
    payload = {
        'isCompleted': True,
        'isFailed': False,
        'result': {
            'success': True,
            'nodeUuid': NODE_UUID,
            'image': {
                'format': 'svg',
                'media_type': 'image/svg+xml',
                'encoding': 'base64',
                'data': 'PHN2Zy8+',
            },
            'rawReport': {'schema': 1, 'tool': 'geocheck'},
            'message': None,
        },
    }
    api = _api()
    api._make_request = AsyncMock(return_value={'response': payload})

    assert await api.get_node_geocheck_result('job-1') == payload


# ============== Данные ноды, нужные для выбора маршрута ==============


def test_parse_node_exposes_ips():
    node = _api()._parse_node(_node_payload())

    assert node.ips == [
        {'ip': '213.176.77.249', 'status': 'OUTBOUND'},
        {'ip': '2a0b:4141:820:140d::2', 'status': 'INBOUND'},
    ]


def test_parse_node_ips_defaults_to_empty_list():
    node = _api()._parse_node(_node_payload(ips=None))

    assert node.ips == []
