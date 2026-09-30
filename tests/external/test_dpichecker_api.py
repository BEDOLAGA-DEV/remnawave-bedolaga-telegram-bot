"""Клиент DPI//CHECKER на записанных ответах: ошибки по code, rejected, 5xx без JSON — шлюз,
ключ в заголовке, Idempotency-Key только у платных POST, пути ручек."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.external.dpichecker_api import DpiCheckerAPI, DpiCheckerAPIError, DpiCheckerGatewayError
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture


def _parse(name: str) -> dict:
    fx = load_dpichecker_fixture(name)
    return DpiCheckerAPI.parse_response(fx['status'], json.dumps(fx['body']))


@pytest.mark.parametrize(
    ('name', 'code', 'status'),
    [
        ('estimate_bad_pop', 'invalid_pops', 400),
        ('estimate_bad_loc', 'invalid_location', 400),
        ('too_many_resources', 'too_many_resources', 400),
        ('ip_bad_pops', 'invalid_pops', 400),
        ('idempotency_conflict', 'idempotency_conflict', 409),
        ('check_404', 'not_found', 404),
        ('noisy_private', 'private_target', 400),
    ],
)
def test_error_codes_come_from_body(name: str, code: str, status: int) -> None:
    with pytest.raises(DpiCheckerAPIError) as info:
        _parse(name)
    assert (info.value.code, info.value.status) == (code, status)
    assert not isinstance(info.value, DpiCheckerGatewayError)


def test_rejected_inputs_are_kept() -> None:
    with pytest.raises(DpiCheckerAPIError) as info:
        _parse('estimate_bad_pop')
    assert info.value.rejected == ['1']


def test_429_carries_retry_after() -> None:
    body = json.dumps({'error': 'slow down', 'code': 'rate_limited', 'retry_after': 7})
    with pytest.raises(DpiCheckerAPIError) as info:
        DpiCheckerAPI.parse_response(429, body)
    assert info.value.retry_after == 7.0


@pytest.mark.parametrize('status', [502, 524])
def test_5xx_without_json_is_gateway(status: int) -> None:
    with pytest.raises(DpiCheckerGatewayError):
        DpiCheckerAPI.parse_response(status, '<html>bad gateway</html>')


def test_success_body_returned_as_is() -> None:
    assert _parse('quota')['monitors']['limit'] == 50


class _FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._text = json.dumps(body)
        self.headers = {'Content-Type': 'application/json'}

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode()


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any):
        self.calls.append({'method': method, 'url': url, **kwargs})

        @asynccontextmanager
        async def _cm():
            yield _FakeResponse(200, {'check_id': 1, 'status': 'pending'})

        return _cm()

    async def close(self) -> None:
        return None


def _api_with(session: _FakeSession) -> DpiCheckerAPI:
    api = DpiCheckerAPI('k-1', base_url='https://dpi.example/api/v1')
    api._session = session  # транспорт подменён — сетевых вызовов нет
    return api


async def test_idempotency_key_only_on_paid_post() -> None:
    session = _FakeSession()
    api = _api_with(session)
    body = {'location': 'russia', 'pop_ids': [1], 'resources': ['8.8.8.8']}
    await api.start_check('ip', body, idempotency_key='a1')
    await api.estimate({'check_type': 'ip', **body})
    assert session.calls[0]['url'] == 'https://dpi.example/api/v1/checks/ip'
    assert session.calls[0]['headers'] == {'Idempotency-Key': 'a1'}
    assert session.calls[1]['headers'] is None


def test_key_sent_in_x_api_key_header() -> None:
    api = DpiCheckerAPI('k-1')
    assert api._headers() == {'X-API-Key': 'k-1', 'Accept': 'application/json'}
    assert 'k-1' not in repr(api)


async def test_unknown_check_type_refused_before_network() -> None:
    session = _FakeSession()
    with pytest.raises(ValueError):
        await _api_with(session).start_check('ssh', {}, idempotency_key='x')
    assert session.calls == []


@pytest.mark.parametrize(
    ('call', 'method', 'path'),
    [
        (lambda api: api.wait_check(5, timeout=500), 'GET', '/checks/5/wait'),
        (lambda api: api.cancel_check(5), 'DELETE', '/checks/5'),
        (lambda api: api.start_probe('1.2.3.0/24', idempotency_key='p'), 'POST', '/checks/probe'),
        (lambda api: api.get_noisy(7), 'GET', '/checks/noisy/7'),
        (lambda api: api.update_monitor(3, {'is_active': False}), 'PATCH', '/monitors/3'),
        (lambda api: api.monitor_runs(3, limit=5), 'GET', '/monitors/3/runs'),
        (lambda api: api.cheremsha(['a.ru', 'b.ru']), 'GET', '/cheremsha'),
        (lambda api: api.list_checks(kind='probe', limit=5), 'GET', '/checks'),
        (lambda api: api.report_json(5), 'GET', '/checks/5/report'),
        (lambda api: api.webhook_deliveries(limit=5), 'GET', '/webhooks/deliveries'),
    ],
)
async def test_methods_hit_expected_paths(call, method: str, path: str) -> None:
    session = _FakeSession()
    await call(_api_with(session))
    assert session.calls[0]['method'] == method
    assert session.calls[0]['url'].endswith(path)


async def test_wait_timeout_clamped_to_service_limit() -> None:
    session = _FakeSession()
    await _api_with(session).wait_check(5, timeout=500)
    assert session.calls[0]['params'] == {'timeout': '120'}


async def test_cheremsha_joins_resources() -> None:
    session = _FakeSession()
    await _api_with(session).cheremsha(['a.ru', 'b.ru'])
    assert session.calls[0]['params'] == {'resource': 'a.ru,b.ru'}


async def test_long_poll_gets_its_own_longer_timeout() -> None:
    session = _FakeSession()
    await _api_with(session).wait_check(5, timeout=60)
    assert session.calls[0]['timeout'].total == 60 + 15
    await _api_with(session).get_check(5)
    assert 'timeout' not in session.calls[1]  # обычные запросы — общий короткий таймаут сессии


def test_default_session_timeout_is_short() -> None:
    from app.external.dpichecker_api import DEFAULT_TIMEOUT

    assert DEFAULT_TIMEOUT <= 30


async def test_account_list_passes_only_given_filters() -> None:
    session = _FakeSession()
    await _api_with(session).list_checks(kind='check', check_type='vpn', limit=10, offset=20)
    assert session.calls[0]['params'] == {'kind': 'check', 'check_type': 'vpn', 'limit': '10', 'offset': '20'}


async def test_report_json_asks_json_format() -> None:
    session = _FakeSession()
    await _api_with(session).report_json(5)
    assert session.calls[0]['params'] == {'format': 'json'}


async def test_deliveries_paged() -> None:
    session = _FakeSession()
    await _api_with(session).webhook_deliveries(limit=5, offset=10)
    assert session.calls[0]['params'] == {'limit': '5', 'offset': '10'}
