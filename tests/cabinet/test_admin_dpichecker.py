"""Ручки /admin/dpichecker: пути зарегистрированы, права read/run, 503 словами при выключенном модуле,
отказ сервиса — его статус и слова, ответы без ключей и тел запроса, аудит запуска, «только мои»."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes import admin_dpichecker
from app.cabinet.schemas.dpichecker import ActionOut, CheckCreate, MonitorPatch, PanelTargetsRequest, ScanCreate
from app.external.dpichecker_api import DpiCheckerAPIError, DpiCheckerGatewayError
from app.services.dpichecker.errors import ActionNotFound, DpiCheckerDisabled, LaunchRefused
from app.services.dpichecker.targets import PanelTarget, PanelTargetError


ADMIN = SimpleNamespace(id=7, telegram_id=1)
BASE = '/cabinet/admin/dpichecker'


def _action(**overrides) -> SimpleNamespace:
    fields = {
        'id': 1,
        'kind': 'check',
        'check_type': 'vpn',
        'remote_id': 5309,
        'status': 'pending',
        'admin_user_id': 7,
        'location': 'russia',
        'pop_count': 10,
        'resource_count': 1,
        'source': 'paste',
        'source_ref': None,
        'label': 'Finland',
        'targets': [{'value': 'vless://secret@x.example:443', 'name': 'Finland'}],
        'request': {'keys': ['vless://secret@x.example:443']},
        'idempotency_key': 'k' * 32,
        'cost_usd': Decimal('0.0400'),
        'refunded_usd': None,
        'error_code': None,
        'delivery_ids': [3],
        'last_run_id': None,
        'created_at': None,
    }
    return SimpleNamespace(**{**fields, **overrides})


@pytest.fixture
def service(monkeypatch):
    fake = SimpleNamespace()
    monkeypatch.setattr(admin_dpichecker, '_service', lambda: fake)
    monkeypatch.setattr(admin_dpichecker.PermissionService, 'log_action', AsyncMock())
    return fake


def test_routes_registered_with_expected_paths_and_permissions():
    from pathlib import Path

    init = Path('app/cabinet/routes/__init__.py').read_text(encoding='utf-8')
    assert 'router.include_router(admin_dpichecker_router)' in init
    routes = {
        (f'/cabinet{route.path}', tuple(sorted(route.methods)))
        for route in admin_dpichecker.router.routes
        if hasattr(route, 'methods')
    }
    expected = [
        (f'{BASE}/status', 'GET'),
        (f'{BASE}/pops', 'GET'),
        (f'{BASE}/checks', 'POST'),
        (f'{BASE}/checks', 'GET'),
        (f'{BASE}/checks/{{action_id}}', 'GET'),
        (f'{BASE}/checks/{{action_id}}', 'DELETE'),
        (f'{BASE}/checks/{{action_id}}/report.csv', 'GET'),
        (f'{BASE}/probe', 'POST'),
        (f'{BASE}/noisy', 'POST'),
        (f'{BASE}/scans/{{action_id}}', 'GET'),
        (f'{BASE}/cheremsha', 'GET'),
        (f'{BASE}/monitors', 'GET'),
        (f'{BASE}/monitors', 'POST'),
        (f'{BASE}/monitors/{{action_id}}', 'PATCH'),
        (f'{BASE}/monitors/{{action_id}}', 'DELETE'),
        (f'{BASE}/spend', 'GET'),
    ]
    for path, method in expected:
        assert (path, (method,)) in routes, (path, method)


def test_money_routes_need_run_permission():
    import inspect

    run_only = {
        'launch_check',
        'cancel_check',
        'launch_probe',
        'launch_noisy',
        'create_monitor',
        'patch_monitor',
        'delete_monitor',
    }
    for name in run_only:
        source = inspect.getsource(getattr(admin_dpichecker, name))
        assert "require_permission('dpichecker:run')" in source, name
    for name in ('get_status', 'list_checks', 'get_check', 'cheremsha', 'list_monitors'):
        assert "require_permission('dpichecker:read')" in inspect.getsource(getattr(admin_dpichecker, name)), name


def test_action_out_hides_keys_and_request():
    dumped = ActionOut.from_action(_action()).model_dump()
    for hidden in ('targets', 'request', 'idempotency_key', 'delivery_ids'):
        assert hidden not in dumped
    assert dumped['target_names'] == ['Finland']
    assert dumped['cost_usd'] == 0.04
    assert 'secret' not in str(dumped)


def test_check_create_limits():
    ok = {'check_type': 'ip', 'location': 'russia', 'pop_ids': [1], 'targets': [{'value': 'a.ru', 'name': 'a'}]}
    CheckCreate(**ok)
    with pytest.raises(ValidationError):
        CheckCreate(**{**ok, 'location': 'mars'})
    with pytest.raises(ValidationError):
        CheckCreate(**{**ok, 'pop_ids': []})
    with pytest.raises(ValidationError):
        CheckCreate(**{**ok, 'targets': [{'value': str(i), 'name': ''} for i in range(51)]})
    with pytest.raises(ValidationError):
        CheckCreate(**{**ok, 'probe_mode': 'maybe'})


def test_monitor_patch_bounds():
    with pytest.raises(ValidationError):
        MonitorPatch(interval_hours=0)
    with pytest.raises(ValidationError):
        MonitorPatch(alert_after_fails=21)
    assert MonitorPatch(is_active=False).model_dump(exclude_none=True) == {'is_active': False}


def test_scan_and_panel_requests_validate():
    with pytest.raises(ValidationError):
        ScanCreate(target='')
    with pytest.raises(ValidationError):
        PanelTargetsRequest(kind='users')


async def test_launch_audits_and_returns_action(service):
    service.launch_check = AsyncMock(return_value=_action())
    body = CheckCreate(check_type='ip', location='russia', pop_ids=[1], targets=[{'value': 'google.com', 'name': 'g'}])
    db = AsyncMock()
    out = await admin_dpichecker.launch_check(body, admin=ADMIN, db=db)
    assert out.id == 1 and out.target_names == ['Finland']
    assert service.launch_check.await_args.kwargs['admin_id'] == 7
    log = admin_dpichecker.PermissionService.log_action.await_args.kwargs
    assert log['action'] == 'dpichecker_check_launch' and log['resource_id'] == '1'
    assert 'google.com' not in str(log['details'])


async def test_list_checks_mine_filters_by_admin(service):
    service.history = AsyncMock(return_value=([_action()], 1))
    out = await admin_dpichecker.list_checks(
        kind='check', check_type=None, mine=True, limit=25, offset=0, admin=ADMIN, db=AsyncMock()
    )
    assert out.total == 1
    assert service.history.await_args.kwargs['admin_user_id'] == 7


async def test_panel_targets_returns_values(service):
    service.panel_targets = AsyncMock(return_value=[PanelTarget(value='fi.example', name='Finland', ref='h1')])
    out = await admin_dpichecker.panel_targets(
        PanelTargetsRequest(kind='hosts', uuids=['h1']), admin=ADMIN, db=AsyncMock()
    )
    assert out.targets[0].model_dump() == {'value': 'fi.example', 'name': 'Finland', 'ref': 'h1'}


@pytest.mark.parametrize(
    ('exc', 'status'),
    [
        (DpiCheckerDisabled('DPI//CHECKER выключен в настройках'), 503),
        (ActionNotFound(), 404),
        (LaunchRefused(code='insufficient_balance', message='Не хватает денег', status=402, rejected=[]), 402),
        (PanelTargetError('У пользователя #5 нет подписки в панели'), 400),
        (DpiCheckerGatewayError(code='timeout', message='t'), 504),
        (DpiCheckerAPIError(code='internal', message='bad', status=500), 502),
        (ValueError('Не выбран пользователь'), 400),
    ],
)
def test_domain_errors_to_http(exc, status):
    http = admin_dpichecker._http(exc)
    assert isinstance(http, HTTPException) and http.status_code == status
    assert 'Traceback' not in str(http.detail)


def test_unexpected_error_is_500_without_details():
    http = admin_dpichecker._http(RuntimeError('boom secret'))
    assert http.status_code == 500 and 'secret' not in str(http.detail)


async def test_report_csv_returns_service_bytes(service):
    service.report_csv = AsyncMock(return_value=(b'a,b\n', 'text/csv; charset=utf-8'))
    response = await admin_dpichecker.report_csv(1, admin=ADMIN, db=AsyncMock())
    assert response.body == b'a,b\n' and response.media_type.startswith('text/csv')


# ---------------------------------------------------------------- находки ревью


@pytest.mark.parametrize('code', ['invalid_api_key', 'ip_not_allowed', 'api_not_unlocked', 'missing_api_key'])
def test_key_problems_are_503_in_words(code):
    http = admin_dpichecker._http(DpiCheckerAPIError(code=code, message='x', status=401))
    assert http.status_code == 503
    refused = admin_dpichecker._http(LaunchRefused(code=code, message='Ключ неверный', status=403, rejected=[]))
    assert refused.status_code == 503


async def test_ip_lookup_rejects_non_ip(service):
    service.ip_lookup = AsyncMock()
    with pytest.raises(HTTPException) as info:
        await admin_dpichecker.ip_lookup('8.8.8.8/../profile', bgp=False, admin=ADMIN)
    assert info.value.status_code == 400
    service.ip_lookup.assert_not_awaited()


async def test_subscription_defaults_to_admin_himself(service):
    service.panel_targets = AsyncMock(return_value=[])
    await admin_dpichecker.panel_targets(PanelTargetsRequest(kind='subscription'), admin=ADMIN, db=AsyncMock())
    assert service.panel_targets.await_args.kwargs['user_id'] == 7


async def test_resubmit_route_audits(service):
    service.resubmit = AsyncMock(return_value=_action(status='pending'))
    out = await admin_dpichecker.resubmit(1, admin=ADMIN, db=AsyncMock())
    assert out.status == 'pending'
    assert admin_dpichecker.PermissionService.log_action.await_args.kwargs['action'] == 'dpichecker_resubmit'
