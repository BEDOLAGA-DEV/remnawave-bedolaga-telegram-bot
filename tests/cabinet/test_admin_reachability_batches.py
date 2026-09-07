"""Роуты пачки проверок: регистрация и права, превью и запуск с аудитом, статус с частичным
результатом по задачам, ошибки домена → HTTP."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_reachability
from app.cabinet.schemas.reachability import BatchCreateRequest
from app.services.reachability.jobs import JobNotCancellable
from app.services.reachability.service import JobNotFound
from tests.cabinet.test_admin_reachability import ADMIN, BASE, BS, _job


def _batch(**overrides) -> SimpleNamespace:
    job = _job(
        id=11,
        status='running',
        phase='retrieving',
        batch_id=3,
        result={'partial': {'done': 1, 'total': 2, 'elapsed_sec': 30, 'legs': []}},
    )
    fields = {
        'id': 3,
        'status': 'running',
        'phase': None,
        'started_by_user_id': 7,
        'scope': {'kind': 'problems', 'host_refs': ['h1']},
        'request': {'units': [], 'dpi': 'on', 'probes': {'tcp': True}, 'sni_hosts': []},
        'total_targets': 1,
        'estimated_kopeks': 640,
        'cost_kopeks': None,
        'error_message': None,
        'created_at': None,
        'started_at': None,
        'finished_at': None,
        'jobs': [job],
    }
    return SimpleNamespace(**{**fields, **overrides})


@pytest.fixture
def service(monkeypatch):
    fake = SimpleNamespace()
    monkeypatch.setattr(admin_reachability, '_service', lambda: fake)
    monkeypatch.setattr(admin_reachability.PermissionService, 'log_action', AsyncMock())
    return fake


def test_batch_routes_registered(registered_paths) -> None:
    assert 'POST' in registered_paths[f'{BASE}/batches/preview']
    assert {'GET', 'POST'} <= registered_paths[f'{BASE}/batches']
    assert 'GET' in registered_paths[f'{BASE}/batches/{{batch_id}}']
    assert 'POST' in registered_paths[f'{BASE}/batches/{{batch_id}}/cancel']


@pytest.mark.parametrize(
    ('endpoint_name', 'permission'),
    [
        ('preview_batch', 'reachability:read'),
        ('create_batch', 'reachability:run'),
        ('list_batches', 'reachability:read'),
        ('get_batch', 'reachability:read'),
        ('cancel_batch', 'reachability:run'),
    ],
)
def test_batch_routes_require_expected_permission(endpoint_name: str, permission: str) -> None:
    endpoint = getattr(admin_reachability, endpoint_name)
    route = next(route for route in admin_reachability.router.routes if route.endpoint is endpoint)
    closures = [
        cell.cell_contents
        for dependency in route.dependant.dependencies
        for cell in getattr(dependency.call, '__closure__', None) or ()
    ]
    assert (permission,) in closures


def test_batch_request_validation() -> None:
    body = BatchCreateRequest(host_refs=['h1'], sni_hosts=['Ads.X5.ru.'])
    assert body.sni_hosts == ['ads.x5.ru'] and body.scope_kind == 'manual' and body.dpi == 'on'
    with pytest.raises(ValueError):
        BatchCreateRequest(host_refs=[])
    with pytest.raises(ValueError):
        BatchCreateRequest(host_refs=['h1'], scope_kind='everything')


@pytest.mark.asyncio
async def test_preview_batch_returns_totals(service) -> None:
    preview = SimpleNamespace(
        targets=[BS],
        chunks=[object(), object()],
        units_resolved=['mts|пфо|on'],
        cost_kopeks=1280,
        estimated_minutes=15,
        warnings=['оценка'],
        balance_kopeks=100_000,
    )
    service.preview_batch = AsyncMock(return_value=preview)
    body = BatchCreateRequest(host_refs=['h1', 'h2'], scope_kind='all')
    out = await admin_reachability.preview_batch(body, admin=ADMIN, db=AsyncMock())
    assert (out.chunks, out.cost_kopeks, out.estimated_minutes, out.balance_kopeks) == (2, 1280, 15, 100_000)
    assert out.targets[0].target_key == 'bs-host.example:9443' and out.warnings == ['оценка']
    service.preview_batch.assert_awaited_once()
    assert service.preview_batch.await_args.args[1]['scope_kind'] == 'all'


@pytest.mark.asyncio
async def test_create_batch_returns_jobs_with_partial_and_audits(service) -> None:
    service.create_batch = AsyncMock(return_value=_batch())
    body = BatchCreateRequest(host_refs=['h1'], units=['*|*|on'], scope_kind='problems')
    db = AsyncMock()
    out = await admin_reachability.create_batch(body, admin=ADMIN, db=db)
    assert out.id == 3 and out.scope['kind'] == 'problems' and out.done_targets == 0
    assert out.jobs[0].partial == {'done': 1, 'total': 2, 'elapsed_sec': 30, 'legs': []}
    assert out.jobs[0].target_keys == ['bs-host.example:9443'] and out.jobs[0].status == 'running'
    service.create_batch.assert_awaited_once_with(db, body.model_dump(), ADMIN.id)
    log = admin_reachability.PermissionService.log_action
    assert (
        log.await_args.kwargs.get('action') == 'reachability_batch_create'
        or 'reachability_batch_create' in log.await_args.args
    )


@pytest.mark.asyncio
async def test_get_batch_counts_done_targets(service) -> None:
    done_job = _job(id=12, status='done', batch_id=3, result={'response': {}})
    service.get_batch = AsyncMock(return_value=_batch(jobs=[done_job, _job(id=13, status='pending', batch_id=3)]))
    out = await admin_reachability.get_batch(3, admin=ADMIN, db=AsyncMock())
    assert out.done_targets == 1 and [job.partial for job in out.jobs] == [None, None]


@pytest.mark.asyncio
async def test_list_batches_paginates(service) -> None:
    service.list_batches = AsyncMock(return_value=([_batch()], 1))
    out = await admin_reachability.list_batches(offset=0, limit=20, admin=ADMIN, db=AsyncMock())
    assert out.total == 1 and out.items[0].id == 3


@pytest.mark.asyncio
async def test_cancel_and_missing_batch_map_domain_errors(service) -> None:
    service.cancel_batch = AsyncMock(side_effect=JobNotCancellable('Проверка уже завершена'))
    with pytest.raises(HTTPException) as conflict:
        await admin_reachability.cancel_batch(3, admin=ADMIN, db=AsyncMock())
    assert conflict.value.status_code == 409
    service.get_batch = AsyncMock(side_effect=JobNotFound(9))
    with pytest.raises(HTTPException) as missing:
        await admin_reachability.get_batch(9, admin=ADMIN, db=AsyncMock())
    assert missing.value.status_code == 404


def test_job_out_carries_batch_id() -> None:
    assert admin_reachability._job_out(_job(batch_id=3)).batch_id == 3
    assert admin_reachability._job_out(_job()).batch_id is None
