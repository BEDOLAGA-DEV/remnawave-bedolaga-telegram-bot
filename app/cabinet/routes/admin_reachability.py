"""Роуты раздела BSCHEKER (bschekbot) в кабинете: тонкий слой над фасадом.

Валидация — pydantic, права — ``reachability:read`` на чтение и ``reachability:run``
на всё, что тратит деньги или меняет данные; исключения домена переводятся в HTTP
в одном месте (:func:`_http`).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ReachabilityBatch, ReachabilityJob, User
from app.external.bschek_api import BschekAPIError
from app.services.permission_service import PermissionService
from app.services.reachability.batches import BatchPreview, batch_done_targets
from app.services.reachability.jobs import JobNotCancellable
from app.services.reachability.pricing import CostLimitExceeded
from app.services.reachability.requests import RequestBuildError
from app.services.reachability.resolver import HostView, NodeView, SubscriptionConfigs, TargetResolutionError
from app.services.reachability.service import (
    JobNotFound,
    PanelUnavailable,
    PreviewResult,
    ReachabilityBusy,
    ReachabilityDisabled,
    ReachabilityService,
    ReachabilityUnhealthy,
    reachability_service,
)
from app.services.reachability.targets import Target, TargetValidationError
from app.services.reachability.units import SelectorError

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.reachability import (
    BatchCreateRequest,
    BatchJobOut,
    BatchListResponse,
    BatchOut,
    BatchPreviewResponse,
    ConfigOut,
    HostsResponse,
    HostTargetOut,
    JobCreateRequest,
    JobListResponse,
    JobOut,
    NodesResponse,
    NodeTargetOut,
    ParsedConfigOut,
    ParsedInputResponse,
    ParseInputRequest,
    PrefOut,
    PrefUpdateRequest,
    PreviewResponse,
    RejectedOut,
    SkippedOut,
    SourceOut,
    StatusResponse,
    SubscriptionConfigsResponse,
    SummaryResponse,
    TargetOut,
    UnitOut,
    UnitsResponse,
)


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/reachability', tags=['Cabinet Admin Reachability'])

BAD_REQUEST_ERRORS = (
    SelectorError,
    TargetValidationError,
    TargetResolutionError,
    RequestBuildError,
    CostLimitExceeded,
    ValueError,
)
REJECTED_PREVIEW_LENGTH = 60


def _service() -> ReachabilityService:
    return reachability_service


def _http(exc: Exception) -> HTTPException:
    """Единственное место перевода исключений домена в HTTP-ответы."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ReachabilityDisabled | PanelUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.reason)
    if isinstance(exc, ReachabilityUnhealthy):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f'{exc.reason} (повтор после {exc.until:%H:%M} UTC)')
    if isinstance(exc, ReachabilityBusy):
        job = exc.job
        detail = f'Уже идёт задача #{job.id} ({job.kind}), запустил пользователь {job.started_by_user_id}'
        return HTTPException(status.HTTP_409_CONFLICT, detail)
    if isinstance(exc, JobNotCancellable):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, JobNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, 'Задача не найдена')
    if isinstance(exc, BAD_REQUEST_ERRORS):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if isinstance(exc, BschekAPIError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, f'bschekbot: {exc.message} [{exc.code}]')
    logger.error('Неожиданная ошибка раздела reachability', error=str(exc), error_type=type(exc).__name__)
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, 'Внутренняя ошибка')


# ============ Сборка ответов ============


def _target_out(target: dict[str, Any] | Target) -> TargetOut:
    data = target.as_dict() if isinstance(target, Target) else dict(target)
    return TargetOut(**{key: value for key, value in data.items() if key in TargetOut.model_fields})


_JOB_DERIVED = ('targets', 'probes', 'sni_hosts', 'batch_id')


def _job_out(job: Any) -> JobOut:
    """Цели задачи — как TargetOut (без raw_link); пробы и SNI-имена — из тела запроса к API."""
    data = {name: getattr(job, name) for name in JobOut.model_fields if name not in _JOB_DERIVED}
    request = getattr(job, 'request', None)
    request = request if isinstance(request, dict) else {}
    probes = request.get('probes')
    return JobOut(
        **data,
        targets=[_target_out(target) for target in job.targets or []],
        probes=dict(probes) if isinstance(probes, dict) else None,
        sni_hosts=[str(name) for name in (request.get('sni_hosts') or [])],
        batch_id=getattr(job, 'batch_id', None),
    )


def _batch_job_out(job: ReachabilityJob | Any) -> BatchJobOut:
    result = job.result if isinstance(job.result, dict) else {}
    return BatchJobOut(
        id=job.id,
        status=job.status,
        phase=job.phase,
        target_keys=[str(target.get('target_key') or '') for target in job.targets or []],
        cost_kopeks=job.cost_kopeks,
        partial=result.get('partial'),
    )


def _batch_out(batch: ReachabilityBatch | Any) -> BatchOut:
    jobs = list(batch.jobs)
    return BatchOut(
        id=batch.id,
        status=batch.status,
        phase=batch.phase,
        scope=dict(batch.scope or {}),
        total_targets=batch.total_targets,
        done_targets=batch_done_targets(jobs),
        estimated_kopeks=batch.estimated_kopeks,
        cost_kopeks=batch.cost_kopeks,
        error_message=batch.error_message,
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        jobs=[_batch_job_out(job) for job in jobs],
    )


def _batch_preview_out(preview: BatchPreview | Any) -> BatchPreviewResponse:
    return BatchPreviewResponse(
        targets=[_target_out(target) for target in preview.targets],
        units_resolved=list(preview.units_resolved),
        chunks=len(preview.chunks),
        cost_kopeks=preview.cost_kopeks,
        estimated_minutes=preview.estimated_minutes,
        warnings=list(preview.warnings),
        balance_kopeks=preview.balance_kopeks,
    )


def _host_out(view: HostView) -> HostTargetOut:
    return HostTargetOut(
        uuid=view.host.uuid,
        remark=view.host.remark,
        address=view.host.address,
        port=view.host.port,
        sni=view.target.sni,
        is_disabled=view.host.is_disabled,
        tag=view.host.tag,
        purpose=view.target.purpose,
        purpose_guessed=view.purpose_guessed,
        excluded=view.excluded,
        node_uuids=view.node_uuids,
        target_key=view.target.target_key,
    )


def _node_out(view: NodeView) -> NodeTargetOut:
    return NodeTargetOut(
        uuid=view.node.uuid,
        name=view.node.name,
        address=view.node.address,
        is_connected=view.node.is_connected,
        is_disabled=view.node.is_disabled,
        host_uuids=view.host_uuids,
        target_key=view.target.target_key,
    )


def _config_out(index: int, target: Target) -> ConfigOut:
    protocol = (target.raw_link or '').split('://', 1)[0] or None
    return ConfigOut(
        index=int(target.ref.get('index', index)),
        protocol=protocol,
        label=target.label,
        address=target.address,
        port=target.port,
        sni=target.sni,
        target_key=target.target_key,
        purpose=target.purpose,
    )


def _rejected_preview(raw: str) -> str:
    """Обрезок без учётных данных: всё до «@» отбрасывается, остаток режется."""
    return raw.split('@')[-1][:REJECTED_PREVIEW_LENGTH]


def _configs_out(configs: SubscriptionConfigs) -> SubscriptionConfigsResponse:
    return SubscriptionConfigsResponse(
        short_uuid=configs.short_uuid,
        configs=[_config_out(index, target) for index, target in enumerate(configs.configs)],
        rejected=[RejectedOut(reason=item.reason, preview=_rejected_preview(item.raw)) for item in configs.rejected],
    )


def _preview_out(preview: PreviewResult) -> PreviewResponse:
    return PreviewResponse(
        kind=preview.kind,
        targets=[_target_out(target) for target in preview.targets],
        units_resolved=preview.units_resolved,
        skipped=SkippedOut(**preview.skipped),
        cost_kopeks=preview.cost_kopeks,
        estimate_is_exact=preview.estimate_is_exact,
        warnings=preview.warnings,
        balance_kopeks=preview.balance_kopeks,
    )


def _csv(value: str | None) -> list[str] | None:
    items = [item.strip() for item in (value or '').split(',') if item.strip()]
    return items or None


async def _audit(
    db: AsyncSession,
    admin: User,
    action: str,
    job: Any,
    details: dict | None = None,
    *,
    resource_type: str = 'reachability_job',
) -> None:
    await PermissionService.log_action(
        db,
        user_id=admin.id,
        action=action,
        resource_type=resource_type,
        resource_id=str(job.id),
        details=details,
    )
    await db.commit()


# ============ Статус, симки, цели ============


@router.get('/status', response_model=StatusResponse)
async def get_status(
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> StatusResponse:
    try:
        return StatusResponse(**await _service().status(db))
    except Exception as exc:
        raise _http(exc) from exc


@router.get('/units', response_model=UnitsResponse)
async def get_units(
    dpi: str | None = Query(default=None),
    operator: str | None = Query(default=None, description='операторы через запятую'),
    region: str | None = Query(default=None, description='округа через запятую, кириллица или код'),
    admin: User = Depends(require_permission('reachability:read')),
) -> UnitsResponse:
    try:
        units = await _service().units(dpi=dpi, operator=_csv(operator), region=_csv(region))
    except Exception as exc:
        raise _http(exc) from exc
    return UnitsResponse(units=[UnitOut(**unit.as_dict()) for unit in units])


@router.get('/targets/hosts', response_model=HostsResponse)
async def get_hosts(
    include_disabled: bool = Query(default=False),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> HostsResponse:
    try:
        views = await _service().hosts(db, include_disabled=include_disabled)
    except Exception as exc:
        raise _http(exc) from exc
    return HostsResponse(items=[_host_out(view) for view in views])


@router.get('/targets/nodes', response_model=NodesResponse)
async def get_nodes(
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> NodesResponse:
    try:
        views = await _service().nodes(db)
    except Exception as exc:
        raise _http(exc) from exc
    return NodesResponse(items=[_node_out(view) for view in views])


@router.get('/targets/subscription', response_model=SubscriptionConfigsResponse)
async def get_subscription_configs(
    short_uuid: str | None = Query(default=None, max_length=255),
    user_id: int | None = Query(default=None, ge=1),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SubscriptionConfigsResponse:
    try:
        configs = await _service().subscription_configs(db, short_uuid=short_uuid, user_id=user_id)
    except Exception as exc:
        raise _http(exc) from exc
    return _configs_out(configs)


@router.post('/targets/parse', response_model=ParsedInputResponse)
async def parse_input(
    body: ParseInputRequest,
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ParsedInputResponse:
    """Поле «Конфиг или подписка»: ссылки, URL подписок, base64 → конфиги с готовыми целями."""
    try:
        parsed = await _service().parse_input(db, body.raw_input)
    except Exception as exc:
        raise _http(exc) from exc
    return ParsedInputResponse(
        configs=[
            ParsedConfigOut(**_config_out(index, item.target).model_dump(), target=item.target_in)
            for index, item in enumerate(parsed.configs)
        ],
        rejected=[RejectedOut(reason=item.reason, preview=_rejected_preview(item.raw)) for item in parsed.rejected],
        sources=[SourceOut(**source) for source in parsed.sources],
    )


@router.put('/targets/prefs', response_model=PrefOut)
async def update_pref(
    body: PrefUpdateRequest,
    admin: User = Depends(require_permission('reachability:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PrefOut:
    try:
        pref = await _service().update_pref(
            db,
            target_kind=body.target_kind,
            target_ref=body.target_ref,
            purpose=body.purpose,
            excluded=body.excluded,
            note=body.note,
            admin_id=admin.id,
        )
    except Exception as exc:
        raise _http(exc) from exc
    return PrefOut.model_validate(pref, from_attributes=True)


# ============ Задачи ============


@router.post('/jobs/preview', response_model=PreviewResponse)
async def preview_job(
    body: JobCreateRequest,
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PreviewResponse:
    try:
        preview = await _service().preview(db, body.model_dump())
    except Exception as exc:
        raise _http(exc) from exc
    return _preview_out(preview)


@router.post('/jobs', response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreateRequest,
    admin: User = Depends(require_permission('reachability:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> JobOut:
    try:
        job = await _service().create_job(db, body.model_dump(), admin.id)
    except Exception as exc:
        raise _http(exc) from exc
    details = {
        'kind': job.kind,
        'units': job.units_resolved,
        'targets': [target.get('target_key') for target in job.targets or []],
        'estimated_kopeks': job.estimated_kopeks,
    }
    await _audit(db, admin, 'reachability_job_create', job, details)
    return _job_out(job)


@router.get('/jobs', response_model=JobListResponse)
async def list_jobs(
    kind: str | None = Query(default=None, max_length=16),
    job_status: str | None = Query(default=None, alias='status', max_length=16),
    target_key: str | None = Query(default=None, max_length=255),
    user_id: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> JobListResponse:
    try:
        items, total = await _service().list_jobs(
            db, kind=kind, status=job_status, target_key=target_key, user_id=user_id, offset=offset, limit=limit
        )
    except Exception as exc:
        raise _http(exc) from exc
    return JobListResponse(items=[_job_out(job) for job in items], total=total, offset=offset, limit=limit)


@router.get('/jobs/{job_id}', response_model=JobOut)
async def get_job(
    job_id: int,
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> JobOut:
    try:
        return _job_out(await _service().get_job(db, job_id))
    except Exception as exc:
        raise _http(exc) from exc


@router.post('/jobs/{job_id}/cancel', response_model=JobOut)
async def cancel_job(
    job_id: int,
    admin: User = Depends(require_permission('reachability:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> JobOut:
    try:
        job = await _service().cancel_job(db, job_id)
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'reachability_job_cancel', job)
    return _job_out(job)


# ============ Пачка проверок ============


@router.post('/batches/preview', response_model=BatchPreviewResponse)
async def preview_batch(
    body: BatchCreateRequest,
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BatchPreviewResponse:
    try:
        preview = await _service().preview_batch(db, body.model_dump())
    except Exception as exc:
        raise _http(exc) from exc
    return _batch_preview_out(preview)


@router.post('/batches', response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchCreateRequest,
    admin: User = Depends(require_permission('reachability:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BatchOut:
    try:
        batch = await _service().create_batch(db, body.model_dump(), admin.id)
    except Exception as exc:
        raise _http(exc) from exc
    details = {
        'scope': (batch.scope or {}).get('kind'),
        'targets': batch.total_targets,
        'estimated_kopeks': batch.estimated_kopeks,
    }
    await _audit(db, admin, 'reachability_batch_create', batch, details, resource_type='reachability_batch')
    return _batch_out(batch)


@router.get('/batches', response_model=BatchListResponse)
async def list_batches(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BatchListResponse:
    try:
        items, total = await _service().list_batches(db, offset=offset, limit=limit)
    except Exception as exc:
        raise _http(exc) from exc
    return BatchListResponse(items=[_batch_out(batch) for batch in items], total=total, offset=offset, limit=limit)


@router.get('/batches/{batch_id}', response_model=BatchOut)
async def get_batch(
    batch_id: int,
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BatchOut:
    try:
        return _batch_out(await _service().get_batch(db, batch_id))
    except Exception as exc:
        raise _http(exc) from exc


@router.post('/batches/{batch_id}/cancel', response_model=BatchOut)
async def cancel_batch(
    batch_id: int,
    admin: User = Depends(require_permission('reachability:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BatchOut:
    try:
        batch = await _service().cancel_batch(db, batch_id)
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'reachability_batch_cancel', batch, resource_type='reachability_batch')
    return _batch_out(batch)


@router.get('/summary/hosts', response_model=SummaryResponse)
async def get_summary(
    dpi: str = Query(default='on', pattern='^(on|off|any)$'),
    admin: User = Depends(require_permission('reachability:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SummaryResponse:
    try:
        return SummaryResponse(**await _service().summary(db, dpi=dpi))
    except Exception as exc:
        raise _http(exc) from exc
