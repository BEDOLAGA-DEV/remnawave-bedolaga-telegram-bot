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
        ('geo_catalog', 'reachability:read'),
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
        # Отказ по нашему запросу — статус сервиса и его слова, не 502 шлюза.
        (BschekAPIError(code='too_many_targets', message='Лимит 10 целей', status=400), 400, 'Лимит 10 целей'),
        (BschekAPIError(code='worker_unavailable', message='нет воркера', status=503), 502, 'worker_unavailable'),
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


# ============== Хосты панели 3.4.3 ==============


@pytest.mark.asyncio
async def test_hosts_route_serializes_a_real_panel_host(service) -> None:
    """Регрессия 2026-09-10 (лог прода): аудит 3.4.3 переименовал у хоста ``tag`` в
    ``tags``, резолвер обновили, а сериализацию для кабинета — нет: список хостов
    в «доступности» падал AttributeError на каждом запросе. Тест идёт через
    настоящий разбор хоста клиентом, а не SimpleNamespace, — двойник с любым
    набором полей такую поломку не видит."""
    from app.external.remnawave_api import RemnaWaveAPI
    from app.services.reachability.resolver import HostView, target_from_host

    host = RemnaWaveAPI._parse_host(
        {
            'uuid': 'h-1',
            'remark': 'Амстердам',
            'address': 'ams.example.net',
            'port': 443,
            'sni': 'cdn.example.net',
            'isDisabled': False,
            'tags': ['БС', 'VIP'],
            'inbound': {'configProfileUuid': 'cp', 'configProfileInboundUuid': 'in'},
        }
    )
    view = HostView(
        host=host,
        target=target_from_host(host, 'bs'),
        purpose_guessed=True,
        excluded=False,
        node_uuids=['n-1'],
    )
    service.hosts = AsyncMock(return_value=[view])

    response = await admin_reachability.get_hosts(include_disabled=False, admin=ADMIN, db=AsyncMock())

    item = response.items[0]
    assert item.uuid == 'h-1' and item.address == 'ams.example.net' and item.port == 443
    assert item.tag == 'БС, VIP'
    assert item.node_uuids == ['n-1'] and item.purpose == 'bs'


def test_parse_input_accepts_ten_thousand_pasted_links() -> None:
    """Владелец: в подписке бывает 10 тысяч серверов — столько же ссылок можно вставить текстом
    (это ~3 МБ), старый потолок поля в 64 КБ резал такой ввод на входе."""
    from app.cabinet.schemas.reachability import ParseInputRequest

    link = 'vless://00000000-0000-4000-8000-000000000001@srv{i}.example:443?security=reality&sni=srv{i}.example#S{i}'
    raw = '\n'.join(link.format(i=i) for i in range(10_000))
    assert len(raw) > 65_536
    assert len(ParseInputRequest(raw_input=raw).raw_input) == len(raw)


def test_configs_out_carries_the_note_and_the_rejection_detail() -> None:
    from app.cabinet.routes.admin_reachability import _configs_out
    from app.services.reachability.links import RejectedLink
    from app.services.reachability.resolver import SubscriptionConfigs

    out = _configs_out(
        SubscriptionConfigs(
            short_uuid='ref-1',
            configs=[],
            rejected=[
                RejectedLink('https://dead.example/abc', 'subscription_failed', detail='Подписка истекла 01.09.2024')
            ],
            note='Подписка отключена в панели',
        )
    )
    assert out.note == 'Подписка отключена в панели'
    assert out.rejected[0].detail == 'Подписка истекла 01.09.2024'


# ============== GEO-РФ ==============


def test_job_create_request_accepts_geo_options_and_rejects_bad_scope() -> None:
    body = JobCreateRequest(
        kind='geo',
        targets=[TargetIn(kind='custom', value='example.com')],
        geo={
            'network': 'mob',
            'scope': {'kind': 'district', 'district': 'cfo'},
            'isp': 'mts',
            'city_limit': 30,
            'probe_mode': 'tcp',
            'heavy': False,
        },
    )
    assert body.geo is not None and body.geo.scope.kind == 'district' and body.geo.city_limit == 30
    assert body.geo.model_dump()['scope'] == {'kind': 'district', 'district': 'cfo', 'region': None, 'cities': []}
    with pytest.raises(ValidationError):
        JobCreateRequest(kind='geo', targets=[TargetIn(kind='custom', value='a')], geo={'scope': {'kind': 'planet'}})
    with pytest.raises(ValidationError):
        JobCreateRequest(kind='geo', targets=[TargetIn(kind='custom', value='a')], geo={'city_limit': -1})
    with pytest.raises(ValidationError):
        JobCreateRequest(kind='geo', targets=[TargetIn(kind='custom', value='a')], geo={'network': 'wifi'})


def test_registered_geo_catalog_route(registered_paths) -> None:
    assert 'GET' in registered_paths[f'{BASE}/geo/catalog']


@pytest.mark.asyncio
async def test_geo_catalog_route_passes_filters_and_shapes_the_answer(service) -> None:
    async def geo_catalog(**filters):
        assert filters == {
            'network': 'res',
            'q': 'Воронеж',
            'isp': None,
            'region': None,
            'district': 'cfo',
            'cities_limit': None,
        }
        return {
            'networks': ['res', 'mob'],
            'districts': [{'code': 'cfo', 'name': 'ЦФО'}, 'szfo'],
            'regions': [{'token': 'voronezh_oblast', 'name': 'Воронежская область', 'district': 'ЦФО'}],
            'isps': [{'token': 'rostelecom', 'name': 'Ростелеком', 'cities': 89}],
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

    service.geo_catalog = geo_catalog
    out = await admin_reachability.geo_catalog(
        network='res',
        q='Воронеж',
        isp=None,
        region=None,
        district='cfo',
        cities_limit=None,
        admin=ADMIN,
        db=AsyncMock(),
    )
    assert out.regions[0].token == 'voronezh_oblast' and out.cities[0].city_ru == 'Воронеж'
    assert out.cities_total == 1 and out.cities_truncated is False and out.isps[0].cities == 89
    assert [d.model_dump() for d in out.districts] == [{'code': 'cfo', 'name': 'ЦФО'}, {'code': 'szfo', 'name': 'СЗФО'}]


@pytest.mark.asyncio
async def test_geo_catalog_route_translates_service_errors(service) -> None:
    service.geo_catalog = AsyncMock(side_effect=ReachabilityDisabled('выключено'))
    with pytest.raises(HTTPException) as exc:
        await admin_reachability.geo_catalog(
            network='res', q=None, isp=None, region=None, district=None, cities_limit=None, admin=ADMIN, db=AsyncMock()
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_preview_out_carries_geo_numbers(service) -> None:
    preview = PreviewResult(
        kind='geo',
        targets=[BS],
        units_resolved=['geo'],
        skipped={},
        cost_kopeks=90,
        estimate_is_exact=False,
        warnings=['Цена GEO — резерв'],
        balance_kopeks=1000,
        request={},
        geo={'n_nodes': 89, 'cap_mb': 0.81, 'reserve_credits': 90, 'estimated_sec': 45, 'max_nodes': 800},
    )
    service.preview = AsyncMock(return_value=preview)
    body = JobCreateRequest(kind='geo', targets=[TargetIn(kind='custom', value='example.com')], geo={})
    out = await admin_reachability.preview_job(body, admin=ADMIN, db=AsyncMock())
    assert out.geo is not None and out.geo.n_nodes == 89 and out.geo.max_nodes == 800 and out.geo.estimated_sec == 45
    assert service.preview.await_args.args[1]['geo']['scope'] == {
        'kind': 'all',
        'district': None,
        'region': None,
        'cities': [],
    }


GEO_ROW = {
    'region': 'voronezh_oblast',
    'region_ru': 'voronezh_oblast',
    'district': '',
    'city': 'voronezh',
    'verdict': 'ok',
    'is_result': True,
    'targets': [],
}
GEO_REGIONS = {'voronezh_oblast': {'name': 'Воронежская область', 'district': 'ЦФО'}}


@pytest.mark.asyncio
async def test_job_out_names_regions_of_geo_rows(service) -> None:
    service.geo_regions = AsyncMock(return_value=GEO_REGIONS)
    job = _job(
        kind='geo',
        status='done',
        result={'rows': [GEO_ROW, {**GEO_ROW, 'region': 'nowhere', 'region_ru': 'nowhere'}], 'summary': {}},
    )
    service.get_job = AsyncMock(return_value=job)
    out = await admin_reachability.get_job(5, admin=ADMIN, db=AsyncMock())
    assert out.result['rows'][0]['region_ru'] == 'Воронежская область' and out.result['rows'][0]['district'] == 'ЦФО'
    assert out.result['rows'][1]['region_ru'] == 'nowhere', 'неизвестный регион остаётся токеном'
    assert job.result['rows'][0]['region_ru'] == 'voronezh_oblast', 'задача в базе не меняется'


@pytest.mark.asyncio
async def test_job_list_names_regions_only_for_geo_jobs(service) -> None:
    service.geo_regions = AsyncMock(return_value=GEO_REGIONS)
    geo = _job(id=6, kind='geo', status='done', result={'rows': [GEO_ROW], 'summary': {}})
    probe = _job(id=7, kind='probe', status='done', result={'rows': [GEO_ROW]})
    service.list_jobs = AsyncMock(return_value=([geo, probe], 2))
    out = await admin_reachability.list_jobs(
        kind=None, job_status=None, target_key=None, user_id=None, offset=0, limit=50, admin=ADMIN, db=AsyncMock()
    )
    assert out.items[0].result['rows'][0]['region_ru'] == 'Воронежская область'
    assert out.items[1].result['rows'][0]['region_ru'] == 'voronezh_oblast'
    assert service.geo_regions.await_count == 1


@pytest.mark.asyncio
async def test_job_out_keeps_tokens_when_catalog_is_unavailable(service) -> None:
    service.geo_regions = AsyncMock(return_value={})
    service.get_job = AsyncMock(return_value=_job(kind='geo', status='done', result={'rows': [GEO_ROW]}))
    out = await admin_reachability.get_job(5, admin=ADMIN, db=AsyncMock())
    assert out.result['rows'][0]['region_ru'] == 'voronezh_oblast'


def test_http_translates_geo_service_errors_into_words() -> None:
    exc = BschekAPIError(code='too_many_nodes', message='raw', status=400, details={'suggested_city_limit': 120})
    http = admin_reachability._http(exc)
    assert http.status_code == 400 and 'потолок 120' in http.detail and 'raw' not in http.detail
    assert (
        admin_reachability._http(BschekAPIError(code='insufficient_credits', message='raw', status=402)).status_code
        == 402
    )
    assert 'Bronze' in admin_reachability._http(BschekAPIError(code='tier_too_low', message='raw', status=403)).detail
    limited = admin_reachability._http(BschekAPIError(code='rate_limited', message='raw', status=429, retry_after=9))
    assert limited.status_code == 429 and '9 с' in limited.detail
    # Сбой сервиса (5xx) и ответ без статуса — по-прежнему 502 с кодом: это не отказ по нашему запросу.
    assert admin_reachability._http(BschekAPIError(code='maintenance', message='raw', status=503)).status_code == 502
    assert admin_reachability._http(BschekAPIError(code='timeout', message='raw')).status_code == 502
