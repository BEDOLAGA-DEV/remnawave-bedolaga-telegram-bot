"""Роуты /admin/reachability: регистрация, права, 503 при выключенной интеграции,
статус без секретов, ошибки домена → HTTP, аудит запуска и отмены, ссылки конфигов не утекают."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes import admin_reachability
from app.cabinet.schemas.reachability import JobCreateRequest, PrefUpdateRequest, TargetIn
from app.external.bschek_api import BschekAPIError
from app.services.reachability.jobs import JobNotCancellable
from app.services.reachability.links import RejectedLink
from app.services.reachability.resolver import SubscriptionConfigs
from app.services.reachability.service import (
    JobNotFound,
    PanelUnavailable,
    PreviewResult,
    ReachabilityBusy,
    ReachabilityDisabled,
    ReachabilityUnhealthy,
)
from app.services.reachability.targets import Target
from app.services.reachability.units import SelectorError


ADMIN = SimpleNamespace(id=7, telegram_id=1)
BASE = '/cabinet/admin/reachability'
LINK = 'vless://00000000-0000-4000-8000-000000000001@bs-host.example:9443?security=reality&sni=whitelisted.example#BS'
BS = Target(
    kind='subscription_config',
    label='BS',
    address='bs-host.example',
    port=9443,
    target_key='bs-host.example:9443',
    sni='whitelisted.example',
    ref={'short_uuid': 'ref-1', 'index': 0},
    purpose='bs',
    raw_link=LINK,
)


def _job(**overrides) -> SimpleNamespace:
    fields = {
        'id': 5,
        'kind': 'probe',
        'status': 'pending',
        'phase': None,
        'trigger': 'manual',
        'started_by_user_id': 7,
        'external_id': None,
        'targets': [BS.as_dict()],
        'units_requested': ['mts'],
        'units_resolved': ['mts|пфо|on'],
        'units_effective': None,
        'skipped': None,
        'dpi': 'on',
        'estimated_kopeks': 18,
        'estimate_is_exact': True,
        'cost_kopeks': None,
        'refunded_kopeks': None,
        'result': None,
        'error_code': None,
        'error_message': None,
        'retryable': None,
        'attempts': 0,
        'created_at': None,
        'started_at': None,
        'finished_at': None,
        'legs': [],
    }
    return SimpleNamespace(**{**fields, **overrides})


@pytest.fixture
def service(monkeypatch):
    fake = SimpleNamespace()
    monkeypatch.setattr(admin_reachability, '_service', lambda: fake)
    monkeypatch.setattr(admin_reachability.PermissionService, 'log_action', AsyncMock())
    return fake


# ============== Регистрация и права ==============


def test_routes_are_registered(registered_paths) -> None:
    assert 'GET' in registered_paths[f'{BASE}/status']
    assert 'GET' in registered_paths[f'{BASE}/units']
    assert 'GET' in registered_paths[f'{BASE}/targets/hosts']
    assert 'GET' in registered_paths[f'{BASE}/targets/nodes']
    assert 'GET' in registered_paths[f'{BASE}/targets/subscription']
    assert 'PUT' in registered_paths[f'{BASE}/targets/prefs']
    assert 'POST' in registered_paths[f'{BASE}/targets/parse']
    assert 'POST' in registered_paths[f'{BASE}/jobs/preview']
    assert {'GET', 'POST'} <= registered_paths[f'{BASE}/jobs']
    assert 'GET' in registered_paths[f'{BASE}/jobs/{{job_id}}']
    assert 'POST' in registered_paths[f'{BASE}/jobs/{{job_id}}/cancel']
    assert 'GET' in registered_paths[f'{BASE}/summary/hosts']


@pytest.mark.parametrize(
    ('endpoint_name', 'permission'),
    [
        ('get_status', 'reachability:read'),
        ('get_units', 'reachability:read'),
        ('get_hosts', 'reachability:read'),
        ('get_nodes', 'reachability:read'),
        ('get_subscription_configs', 'reachability:read'),
        ('parse_input', 'reachability:read'),
        ('update_pref', 'reachability:run'),
        ('preview_job', 'reachability:read'),
        ('create_job', 'reachability:run'),
        ('list_jobs', 'reachability:read'),
        ('get_job', 'reachability:read'),
        ('cancel_job', 'reachability:run'),
        ('get_summary', 'reachability:read'),
    ],
)
def test_routes_require_expected_permission(endpoint_name: str, permission: str) -> None:
    endpoint = getattr(admin_reachability, endpoint_name)
    route = next(route for route in admin_reachability.router.routes if route.endpoint is endpoint)
    closures = [
        cell.cell_contents
        for dependency in route.dependant.dependencies
        for cell in getattr(dependency.call, '__closure__', None) or ()
    ]
    assert (permission,) in closures


# ============== Валидация входа ==============


def test_target_in_validation() -> None:
    with pytest.raises(ValidationError):
        TargetIn(kind='host')
    with pytest.raises(ValidationError):
        TargetIn(kind='subscription_config', short_uuid='x')
    with pytest.raises(ValidationError):
        TargetIn(kind='cidr', value='  ')
    assert TargetIn(kind='custom', value='1.1.1.1').value == '1.1.1.1'
    with pytest.raises(ValidationError):
        JobCreateRequest(kind='probe', targets=[])
    with pytest.raises(ValidationError):
        JobCreateRequest(kind='probe', targets=[TargetIn(kind='custom', value='1.1.1.1')], dpi='maybe')
    body = JobCreateRequest(kind='probe', targets=[TargetIn(kind='custom', value='1.1.1.1')])
    assert body.model_dump()['probes'] == {'icmp': False, 'tcp': True, 'sni': True}


# ============== Перевод ошибок ==============


async def test_status_maps_service_dict(service) -> None:
    service.status = AsyncMock(
        return_value={
            'enabled': True,
            'configured': True,
            'healthy': True,
            'balance_kopeks': 100018,
            'tier': 'gold',
            'active_jobs': [],
            'reference': {'short_uuid': 'r', 'configs': 3, 'rejected': 1, 'error': None},
            'cost_limit_kopeks': 0,
            'cores': {'stable': '26.3.27', 'prerelease': '26.7.11'},
        }
    )
    response = await admin_reachability.get_status(admin=ADMIN, db=None)
    assert (response.balance_kopeks, response.tier, response.reference.configs) == (100018, 'gold', 3)
    assert response.cores == {'stable': '26.3.27', 'prerelease': '26.7.11'}
    assert 'webhook_secret' not in response.model_dump()


def _probe_body(**overrides) -> JobCreateRequest:
    return JobCreateRequest(**{'kind': 'probe', 'targets': [TargetIn(kind='custom', value='1.1.1.1')], **overrides})


@pytest.mark.parametrize(
    ('error', 'code', 'fragment'),
    [
        (ReachabilityDisabled('выключено'), 503, 'выключено'),
        (ReachabilityUnhealthy('ключ отозван', datetime(2026, 9, 5, 12, 30, tzinfo=UTC)), 503, '12:30'),
        (PanelUnavailable('панель лежит'), 503, 'панель'),
        (SelectorError('Неизвестные симки: nokia'), 400, 'nokia'),
        (ValueError('Для скана нужна подсеть /24'), 400, '/24'),
        (BschekAPIError(code='too_many_targets', message='Лимит 10 целей', status=400), 502, 'too_many_targets'),
        (RuntimeError('boom'), 500, 'Внутренняя'),
        (HTTPException(418, 'чайник'), 418, 'чайник'),
    ],
)
async def test_preview_errors_are_translated(service, error: Exception, code: int, fragment: str) -> None:
    service.preview = AsyncMock(side_effect=error)
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.preview_job(_probe_body(units=['nokia']), admin=ADMIN, db=None)
    assert exc.value.status_code == code and fragment in exc.value.detail


async def test_busy_is_409_with_job_reference(service) -> None:
    active = SimpleNamespace(id=42, kind='vless', started_by_user_id=3, started_at=None)
    service.create_job = AsyncMock(side_effect=ReachabilityBusy(active))
    body = JobCreateRequest(kind='vless', targets=[TargetIn(kind='subscription_config', short_uuid='s', index=0)])
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.create_job(body, admin=ADMIN, db=None)
    assert exc.value.status_code == 409 and '#42' in exc.value.detail


async def test_cancel_not_cancellable_is_409_and_not_found_is_404(service) -> None:
    service.cancel_job = AsyncMock(side_effect=JobNotCancellable('нельзя'))
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.cancel_job(1, admin=ADMIN, db=None)
    assert exc.value.status_code == 409
    service.get_job = AsyncMock(side_effect=JobNotFound(9))
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.get_job(9, admin=ADMIN, db=None)
    assert exc.value.status_code == 404


# ============== Аудит и сборка ответов ==============


async def test_create_job_logs_audit_and_hides_raw_links(service) -> None:
    service.create_job = AsyncMock(return_value=_job())
    db = AsyncMock()
    response = await admin_reachability.create_job(_probe_body(), admin=ADMIN, db=db)
    assert response.id == 5 and response.targets[0].target_key == 'bs-host.example:9443'
    assert 'raw_link' not in response.model_dump()['targets'][0]
    assert LINK not in response.model_dump_json()
    service.create_job.assert_awaited_once()
    assert service.create_job.await_args.args[2] == 7
    admin_reachability.PermissionService.log_action.assert_awaited_once()
    kwargs = admin_reachability.PermissionService.log_action.await_args.kwargs
    assert (kwargs['action'], kwargs['resource_type'], kwargs['resource_id']) == (
        'reachability_job_create',
        'reachability_job',
        '5',
    )
    assert kwargs['details']['targets'] == ['bs-host.example:9443']
    db.commit.assert_awaited_once()


async def test_cancel_logs_audit(service) -> None:
    service.cancel_job = AsyncMock(return_value=_job(kind='vless', status='running', phase='cancelling'))
    response = await admin_reachability.cancel_job(5, admin=ADMIN, db=AsyncMock())
    assert response.phase == 'cancelling'
    assert admin_reachability.PermissionService.log_action.await_args.kwargs['action'] == 'reachability_job_cancel'


async def test_list_jobs_passes_filters_and_paginates(service) -> None:
    service.list_jobs = AsyncMock(return_value=([_job()], 1))
    response = await admin_reachability.list_jobs(
        kind='probe', job_status='done', target_key='k', user_id=3, offset=10, limit=5, admin=ADMIN, db=None
    )
    assert (response.total, response.offset, response.limit, response.items[0].id) == (1, 10, 5, 5)
    assert service.list_jobs.await_args.kwargs == {
        'kind': 'probe',
        'status': 'done',
        'target_key': 'k',
        'user_id': 3,
        'offset': 10,
        'limit': 5,
    }


async def test_units_splits_csv_filters(service) -> None:
    service.units = AsyncMock(return_value=[])
    await admin_reachability.get_units(dpi='on', operator='mts, tele2', region='', admin=ADMIN)
    assert service.units.await_args.kwargs == {'dpi': 'on', 'operator': ['mts', 'tele2'], 'region': None}


async def test_subscription_configs_hide_credentials(service) -> None:
    stub = f'vless://{"1" * 36}@0.0.0.0:1?security=none#stub'
    service.subscription_configs = AsyncMock(
        return_value=SubscriptionConfigs(short_uuid='ref-1', configs=[BS], rejected=[RejectedLink(stub, 'stub')])
    )
    response = await admin_reachability.get_subscription_configs(short_uuid='ref-1', user_id=None, admin=ADMIN, db=None)
    config = response.configs[0]
    assert (config.index, config.protocol, config.label, config.purpose) == (0, 'vless', 'BS', 'bs')
    assert response.rejected[0].preview == '0.0.0.0:1?security=none#stub' and '1' * 36 not in response.model_dump_json()
    assert LINK not in response.model_dump_json()


async def test_preview_response_omits_request_body(service) -> None:
    preview = PreviewResult(
        kind='vless',
        targets=[BS],
        units_resolved=['tele2|цфо|on'],
        skipped={'dpi_off': [], 'unavailable': [], 'unknown': [], 'blocked_targets': []},
        cost_kopeks=110,
        estimate_is_exact=False,
        warnings=['оценка'],
        balance_kopeks=100018,
        request={'raw_input': LINK},
    )
    service.preview = AsyncMock(return_value=preview)
    body = JobCreateRequest(kind='vless', targets=[TargetIn(kind='subscription_config', short_uuid='ref-1', index=0)])
    response = await admin_reachability.preview_job(body, admin=ADMIN, db=None)
    assert response.cost_kopeks == 110 and LINK not in response.model_dump_json()


async def test_update_pref_calls_service_with_admin(service) -> None:
    service.update_pref = AsyncMock(
        return_value=SimpleNamespace(target_kind='host', target_ref='h', purpose='bs', excluded=False, note=None)
    )
    body = PrefUpdateRequest(target_kind='host', target_ref='h', purpose='bs')
    response = await admin_reachability.update_pref(body, admin=ADMIN, db=AsyncMock())
    assert response.purpose == 'bs'
    assert service.update_pref.await_args.kwargs['admin_id'] == 7


async def test_summary_maps_rows_and_units(service) -> None:
    now = datetime.now(UTC)
    service.summary = AsyncMock(
        return_value={
            'dpi': 'on',
            'units': [
                {'op_key': 'mts|пфо|on', 'operator': 'mts', 'in_catalog': True},
                {'op_key': 'old|цфо|on', 'in_catalog': False},
            ],
            'rows': [
                {
                    'target_key': 'bs-host.example:9443',
                    'kind': 'host',
                    'ref': 'h-bs',
                    'label': 'BS',
                    'purpose': 'bs',
                    'purpose_guessed': True,
                    'in_panel': True,
                    'cells': {
                        'mts|пфо|on': {
                            'verdict': 'reachable',
                            'matches_expectation': True,
                            'checked_at': now,
                            'job_id': 1,
                        }
                    },
                }
            ],
            'panel_error': None,
        }
    )
    response = await admin_reachability.get_summary(dpi='on', admin=ADMIN, db=None)
    assert [u.in_catalog for u in response.units] == [True, False]
    assert response.rows[0].cells['mts|пфо|on'].verdict == 'reachable' and response.rows[0].purpose_guessed


# ============== SNI: дефолт в статусе и свои имена в запросе ==============


async def test_status_exposes_default_sni(service) -> None:
    service.status = AsyncMock(
        return_value={
            'enabled': True,
            'configured': True,
            'healthy': True,
            'active_jobs': [],
            'reference': None,
            'cost_limit_kopeks': 0,
            'cores': {},
            'default_sni': 'ads.x5.ru',
        }
    )
    response = await admin_reachability.get_status(admin=ADMIN, db=None)
    assert response.default_sni == 'ads.x5.ru'


def test_job_request_accepts_up_to_five_sni_hosts_and_normalizes_them() -> None:
    body = _probe_body(sni_hosts=[' Ads.X5.ru ', 'vk.com', 'ads.x5.ru'])
    assert body.sni_hosts == ['ads.x5.ru', 'vk.com']
    assert _probe_body().sni_hosts == []
    with pytest.raises(ValidationError):
        _probe_body(sni_hosts=[f'h{i}.example' for i in range(6)])
    with pytest.raises(ValidationError):
        _probe_body(sni_hosts=['not a host'])


# ============== Поле «Конфиг или подписка» и пробы в истории ==============


def test_target_in_accepts_subscription_config_by_url() -> None:
    item = TargetIn(kind='subscription_config', url='https://sub.example/x', index=0, target_key='a.example:443')
    assert item.url == 'https://sub.example/x'
    with pytest.raises(ValidationError):
        TargetIn(kind='subscription_config', index=0)


async def test_parse_input_route_maps_configs_and_hides_raw_links(service) -> None:
    from app.cabinet.schemas.reachability import ParseInputRequest
    from app.services.reachability.service import ParsedConfig, ParsedInput

    stub = f'vless://{"1" * 36}@0.0.0.0:1?security=none#stub'
    target_in = {'kind': 'subscription_config', 'url': 'https://sub.example/x', 'index': 0, 'target_key': BS.target_key}
    service.parse_input = AsyncMock(
        return_value=ParsedInput(
            configs=[ParsedConfig(target=BS, target_in=target_in)],
            rejected=[RejectedLink(stub, 'stub')],
            sources=[{'kind': 'subscription', 'label': 'https://sub.example/x', 'count': 1}],
        )
    )
    response = await admin_reachability.parse_input(
        ParseInputRequest(raw_input='https://sub.example/x'), admin=ADMIN, db=None
    )
    assert response.configs[0].target == target_in and response.configs[0].label == 'BS'
    assert response.sources[0].count == 1 and response.rejected[0].reason == 'stub'
    assert LINK not in response.model_dump_json() and '1' * 36 not in response.model_dump_json()


async def test_job_out_exposes_probes_and_sni_hosts_from_request(service) -> None:
    service.get_job = AsyncMock(
        return_value=_job(request={'probes': {'icmp': False, 'tcp': True, 'sni': True}, 'sni_hosts': ['ads.x5.ru']})
    )
    response = await admin_reachability.get_job(5, admin=ADMIN, db=None)
    assert response.probes == {'icmp': False, 'tcp': True, 'sni': True} and response.sni_hosts == ['ads.x5.ru']
    service.get_job = AsyncMock(return_value=_job())
    bare = await admin_reachability.get_job(5, admin=ADMIN, db=None)
    assert bare.probes is None and bare.sni_hosts == []
