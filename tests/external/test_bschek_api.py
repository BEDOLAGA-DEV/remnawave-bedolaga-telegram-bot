"""Клиент bschekbot API v1 на записанных ответах живого сервиса.

Что закреплено: единый конверт ошибок (включая коды, которых нет в контракте),
ответ без конверта = отдельный класс ошибки (524 за Cloudflare — деньги списаны,
результат надо забирать повтором ключа), сборка query для /operators без потери
кириллицы и сокрытие webhook_secret.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.external.bschek_api import (
    BschekAPI,
    BschekAPIError,
    BschekGatewayError,
    build_operators_params,
)
from tests.fixtures.bschek_fixtures import iter_bschek_fixtures, load_bschek_fixture


def _parse(name: str) -> dict:
    fx = load_bschek_fixture(name)
    return BschekAPI.parse_response(fx['status'], json.dumps(fx['body'], ensure_ascii=False), fx['headers'])


@pytest.mark.parametrize(
    ('name', 'code', 'status', 'retryable'),
    [
        ('auth_bad', 'unauthenticated', 401, False),
        ('auth_none', 'unauthenticated', 401, False),
        ('method_405', 'method_not_allowed', 405, False),
        ('pv_conflict', 'no_dpi_on', 400, False),
        ('pv_unknown_op', 'worker_unavailable', 503, True),
        ('pv_garbage', 'invalid_request', 422, False),
        ('pv_old_format', 'unknown_operator', 400, False),
        ('pv_no_probes', 'no_probes', 400, False),
        ('pv_11_targets', 'too_many_targets', 400, False),
        ('rl2_b', 'rate_limited', 429, True),
        ('v1_second', 'test_in_progress', 409, True),
        ('s1_second', 'scan_in_progress', 409, True),
        ('pF_same_key_while_running', 'request_in_progress', 409, False),
        ('p1_reused', 'idempotency_key_reused', 409, False),
        ('p_noidem', 'idempotency_key_required', 400, False),
        ('p_blocked', 'blocked_target', 400, False),
        ('v_noconfigs', 'parse_failed', 400, False),
        ('v_suburl', 'subscription_not_supported', 400, False),
        ('v_too_many', 'too_many_configs', 400, False),
        ('v_too_large', 'input_too_large', 400, False),
        ('s_notfound', 'not_found', 404, False),
        ('v_cancel_done', 'not_found', 404, False),
        ('sB_cancel_again', 'not_running', 409, False),
        ('v2_cancel_again', 'cannot_cancel_running', 409, True),
        ('sv_not24', 'cidr_too_wide', 400, False),
        ('sv_webhook', 'webhooks_disabled', 400, False),
    ],
)
def test_error_envelope_is_mapped(name: str, code: str, status: int, retryable: bool) -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse(name)
    assert not isinstance(exc.value, BschekGatewayError)
    assert (exc.value.code, exc.value.status, exc.value.retryable) == (code, status, retryable)
    assert exc.value.message


def test_no_dpi_on_carries_skipped_units_in_details() -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse('pv_conflict')
    assert exc.value.details['skipped_dpi_off'][0]['op_key'] == 'yota|уфо|off'


def test_rate_limited_exposes_retry_after() -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse('rl2_b')
    assert exc.value.retry_after == pytest.approx(1.0)


def test_validation_422_keeps_fields() -> None:
    with pytest.raises(BschekAPIError) as exc:
        _parse('pv_garbage')
    assert exc.value.details['fields'][0]['type'] == 'json_invalid'


def test_cloudflare_524_without_body_is_gateway_error() -> None:
    fx = load_bschek_fixture('pF_fleet')
    with pytest.raises(BschekGatewayError) as exc:
        BschekAPI.parse_response(fx['status'], '', fx['headers'])
    assert exc.value.status == 524
    assert exc.value.retryable is True


def test_html_502_is_gateway_error() -> None:
    with pytest.raises(BschekGatewayError):
        BschekAPI.parse_response(502, '<html>bad gateway</html>', {'Content-Type': 'text/html'})


def test_success_body_is_returned_as_is() -> None:
    body = _parse('p2_replay')
    assert body['outcome'] == 'done'
    assert body['cost_credits'] == 260
    assert set(body['by_target']) == {'eu-host.example', 'bs-host.example:9443'}


def test_no_dpi_on_race_with_200_is_not_an_error() -> None:
    body = BschekAPI.parse_response(200, json.dumps({'outcome': 'no_dpi_on', 'skipped_dpi_off': []}), {})
    assert body['outcome'] == 'no_dpi_on'


def test_every_recorded_error_fixture_parses_to_a_code() -> None:
    """Сторож: новый записанный ответ с конвертом ошибки обязан разбираться."""
    seen = 0
    for _name, fx in iter_bschek_fixtures():
        if isinstance(fx['body'], dict) and 'error' in fx['body']:
            with pytest.raises(BschekAPIError) as exc:
                BschekAPI.parse_response(fx['status'], json.dumps(fx['body'], ensure_ascii=False), fx['headers'])
            assert exc.value.code
            seen += 1
    assert seen >= 25


def test_operators_params_join_lists_and_keep_cyrillic() -> None:
    params = build_operators_params(dpi='on', operator=['mts', 'beeline'], region=['цфо'], probeable=True)
    assert params == {'dpi': 'on', 'operator': 'mts,beeline', 'region': 'цфо', 'probeable': 'true'}
    assert build_operators_params() == {}


async def test_get_openapi_reads_spec_from_api_root(monkeypatch: pytest.MonkeyPatch) -> None:
    api = BschekAPI(api_key='bsk_live_test')

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict:
        assert (method, path) == ('GET', '/openapi.json')
        return {'openapi': '3.1.0', 'paths': {}}

    monkeypatch.setattr(api, '_request', fake_request)
    assert (await api.get_openapi())['openapi'] == '3.1.0'


async def test_account_hides_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    api = BschekAPI(api_key='bsk_live_test')
    fx = load_bschek_fixture('account')

    async def fake_request(method: str, path: str, **kwargs: Any) -> dict:
        assert (method, path) == ('GET', '/account')
        return dict(fx['body'])

    monkeypatch.setattr(api, '_request', fake_request)
    account = await api.get_account()
    assert 'webhook_secret' not in account
    assert account['balance_total'] == fx['body']['balance_total']


@pytest.mark.parametrize(
    ('method_name', 'args', 'expected'),
    [
        ('probe', ({'target': 'x'}, 'k1'), ('POST', '/probe', {'target': 'x'}, 'k1')),
        ('preview_probe', ({'target': 'x'},), ('POST', '/probe/preview', {'target': 'x'}, None)),
        ('start_scan', ({'cidr': 'c'}, 'k2'), ('POST', '/scans', {'cidr': 'c'}, 'k2')),
        ('preview_scan', ({'cidr': 'c'},), ('POST', '/scans/preview', {'cidr': 'c'}, None)),
        ('get_scan', (5,), ('GET', '/scans/5', None, None)),
        ('cancel_scan', (5,), ('POST', '/scans/5/cancel', None, None)),
        ('cancel_probe', ('k9',), ('POST', '/probe/cancel', None, 'k9')),
        ('start_vless', ({'raw_input': 'v'}, 'k3'), ('POST', '/vless', {'raw_input': 'v'}, 'k3')),
        ('get_vless', (7,), ('GET', '/vless/7', None, None)),
        ('cancel_vless', (7,), ('POST', '/vless/7/cancel', None, None)),
    ],
)
async def test_methods_hit_expected_paths(monkeypatch: pytest.MonkeyPatch, method_name, args, expected) -> None:
    api = BschekAPI(api_key='bsk_live_test')
    calls: list[tuple] = []

    async def fake_request(method: str, path: str, *, params=None, json_body=None, idempotency_key=None) -> dict:
        calls.append((method, path, json_body, idempotency_key))
        return {}

    monkeypatch.setattr(api, '_request', fake_request)
    await getattr(api, method_name)(*args)
    assert calls == [expected]


def test_api_key_never_appears_in_repr() -> None:
    api = BschekAPI(api_key='bsk_live_secret')
    assert 'bsk_live_secret' not in repr(api)
