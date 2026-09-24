"""Ручки раздела DPI//CHECKER в кабинете: тонкий слой над фасадом.

Права — ``dpichecker:read`` на чтение и бесплатные справки, ``dpichecker:run`` на всё, что тратит
деньги или меняет мониторы. Исключения домена переводятся в HTTP в одном месте (:func:`_http`).
"""

from __future__ import annotations

import ipaddress
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.external.dpichecker_api import DpiCheckerAPIError, DpiCheckerGatewayError
from app.services.dpichecker.errors import ActionNotFound, DpiCheckerDisabled, LaunchRefused, human_error
from app.services.dpichecker.service import DpiCheckerService, dpichecker_service
from app.services.dpichecker.targets import PanelTargetError
from app.services.permission_service import PermissionService

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.dpichecker import (
    ActionListResponse,
    ActionOut,
    CheckCreate,
    CheckResponse,
    DownloadLinkOut,
    EstimateRequest,
    Location,
    MonitorCreate,
    MonitorListResponse,
    MonitorPatch,
    OptimalResponse,
    PanelTargetOut,
    PanelTargetsRequest,
    PanelTargetsResponse,
    ParseRequest,
    PopsResponse,
    ScanCreate,
    ScanResponse,
    StatusResponse,
)
from .media import _verify_media_token, make_media_token


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/dpichecker', tags=['Cabinet Admin DPI//CHECKER'])
# Скачивание по подписанной ссылке — без Authorization: Telegram.WebApp.downloadFile качает URL сам.
download_router = APIRouter(prefix='/dpichecker', tags=['Cabinet Admin DPI//CHECKER'])

DownloadKind = Literal['report', 'noisy']
DOWNLOAD_TTL_SECONDS = 5 * 60
TELEGRAM_WEB_ORIGIN = 'https://web.telegram.org'

GATEWAY_DETAIL = (
    'DPI//CHECKER не ответил. Если это был запуск — откройте его в истории и нажмите «Спросить ещё раз»: '
    'повтор идёт с тем же ключом и второй раз денег не спишет'
)
# Проблема с ключом — раздел неработоспособен, а не «нет прав» у админа кабинета.
KEY_PROBLEM_CODES = frozenset(
    {'invalid_api_key', 'missing_api_key', 'ip_not_allowed', 'api_not_unlocked', 'account_banned'}
)
CHEREMSHA_MAX = 20
REFUSAL_STATUSES = frozenset({400, 402, 403, 404, 409, 429})


def _service() -> DpiCheckerService:
    return dpichecker_service


def _http(exc: Exception) -> HTTPException:
    """Единственное место перевода исключений домена в HTTP-ответы."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, DpiCheckerDisabled):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.reason)
    if isinstance(exc, ActionNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, 'Не найдено')
    if isinstance(exc, LaunchRefused | DpiCheckerAPIError) and exc.code in KEY_PROBLEM_CODES:
        text = exc.message if isinstance(exc, LaunchRefused) else human_error(exc)
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, text)
    if isinstance(exc, LaunchRefused):
        code = exc.status if exc.status in REFUSAL_STATUSES else status.HTTP_400_BAD_REQUEST
        return HTTPException(code, exc.message)
    if isinstance(exc, PanelTargetError | ValueError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if isinstance(exc, DpiCheckerGatewayError):
        return HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, GATEWAY_DETAIL)
    if isinstance(exc, DpiCheckerAPIError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, human_error(exc))
    logger.error('Неожиданная ошибка раздела DPI//CHECKER', error=str(exc)[:300], error_type=type(exc).__name__)
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, 'Внутренняя ошибка')


async def _audit(db: AsyncSession, admin: User, action: str, resource_id: int, details: dict) -> None:
    await PermissionService.log_action(
        db, user_id=admin.id, action=action, resource_type='dpichecker', resource_id=str(resource_id), details=details
    )
    await db.commit()


def _file(data: bytes, content_type: str, filename: str, *, headers: dict[str, str] | None = None) -> Response:
    return Response(
        content=data,
        media_type=content_type,
        headers={'Content-Disposition': f'attachment; filename="{filename}"', **(headers or {})},
    )


def _download_name(kind: str, action_id: int) -> str:
    return f'dpichecker_{action_id}.csv' if kind == 'report' else f'dpichecker_noisy_{action_id}.csv'


# ============ Статус и справочники ============


@router.get('/status', response_model=StatusResponse)
async def get_status(admin: User = Depends(require_permission('dpichecker:read'))) -> StatusResponse:
    try:
        return StatusResponse(**await _service().status())
    except Exception as exc:
        raise _http(exc) from exc


@router.get('/pops', response_model=PopsResponse)
async def get_pops(
    location: Location = Query(default='russia'), admin: User = Depends(require_permission('dpichecker:read'))
) -> PopsResponse:
    try:
        return PopsResponse(**await _service().pops(location))
    except Exception as exc:
        raise _http(exc) from exc


@router.get('/pops/optimal', response_model=OptimalResponse)
async def get_optimal(
    location: Location = Query(default='russia'), admin: User = Depends(require_permission('dpichecker:read'))
) -> OptimalResponse:
    try:
        return OptimalResponse(pop_ids=await _service().optimal(location))
    except Exception as exc:
        raise _http(exc) from exc


@router.get('/tariffs')
async def get_tariffs(admin: User = Depends(require_permission('dpichecker:read'))) -> dict:
    try:
        return await _service().tariffs()
    except Exception as exc:
        raise _http(exc) from exc


@router.post('/parse')
async def parse(body: ParseRequest, admin: User = Depends(require_permission('dpichecker:read'))) -> dict:
    try:
        return await _service().parse(body.check_type, body.text)
    except Exception as exc:
        raise _http(exc) from exc


@router.post('/targets/panel', response_model=PanelTargetsResponse)
async def panel_targets(
    body: PanelTargetsRequest,
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PanelTargetsResponse:
    try:
        # Без выбранного пользователя — подписка по умолчанию из настроек (как у BSCHEKER).
        found = await _service().panel_targets(db, kind=body.kind, user_id=body.user_id, uuids=body.uuids)
    except Exception as exc:
        raise _http(exc) from exc
    return PanelTargetsResponse(targets=[PanelTargetOut(value=t.value, name=t.name, ref=t.ref) for t in found])


@router.post('/estimate')
async def estimate(body: EstimateRequest, admin: User = Depends(require_permission('dpichecker:read'))) -> dict:
    try:
        return await _service().estimate(body.check_type, body.location, body.pop_ids, body.resources)
    except Exception as exc:
        raise _http(exc) from exc


# ============ Проверки ============


@router.post('/checks', response_model=ActionOut)
async def launch_check(
    body: CheckCreate,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionOut:
    try:
        action = await _service().launch_check(
            db,
            admin_id=admin.id,
            check_type=body.check_type,
            location=body.location,
            pop_ids=body.pop_ids,
            targets=[target.model_dump() for target in body.targets],
            source=body.source,
            source_ref=body.source_ref,
            label=body.label,
            probe_mode=body.probe_mode,
        )
    except Exception as exc:
        raise _http(exc) from exc
    details = {
        'check_type': body.check_type,
        'location': body.location,
        'pops': len(body.pop_ids),
        'resources': len(body.targets),
        'source': body.source,
    }
    await _audit(db, admin, 'dpichecker_check_launch', action.id, details)
    return ActionOut.from_action(action)


@router.get('/checks', response_model=ActionListResponse)
async def list_checks(
    kind: Literal['check', 'probe', 'noisy', 'monitor'] | None = Query(default=None),
    check_type: Literal['vpn', 'ip', 'mtproto'] | None = Query(default=None),
    mine: bool = Query(default=False),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionListResponse:
    page = await _service().history(
        db, kind=kind, check_type=check_type, admin_user_id=admin.id if mine else None, limit=limit, offset=offset
    )
    names = page['admin_names']
    return ActionListResponse(
        items=[ActionOut.from_action(item, names.get(item.admin_user_id)) for item in page['items']],
        total=page['total'],
        counts=page['counts'],
    )


@router.get('/checks/{action_id}', response_model=CheckResponse)
async def get_check(
    action_id: int,
    wait: int = Query(default=0, ge=0, le=60),
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CheckResponse:
    try:
        view = await _service().get_check(db, action_id, wait=wait)
    except Exception as exc:
        raise _http(exc) from exc
    return CheckResponse(action=ActionOut.from_action(view['action']), check=view['check'])


@router.delete('/checks/{action_id}', response_model=ActionOut)
async def cancel_check(
    action_id: int,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionOut:
    try:
        action = await _service().cancel_check(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'dpichecker_check_cancel', action.id, {'refunded_usd': str(action.refunded_usd)})
    return ActionOut.from_action(action)


@router.post('/checks/{action_id}/resubmit', response_model=ActionOut)
async def resubmit(
    action_id: int,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionOut:
    """Сервис не ответил на запуск — спросить ещё раз тем же ключом (второго списания не будет)."""
    try:
        action = await _service().resubmit(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'dpichecker_resubmit', action.id, {'kind': action.kind})
    return ActionOut.from_action(action)


@router.get('/checks/{action_id}/report.csv')
async def report_csv(
    action_id: int,
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    try:
        data, content_type = await _service().report_csv(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    return _file(data, content_type, _download_name('report', action_id))


@router.get('/checks/{action_id}/map.png')
async def check_map(
    action_id: int,
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    try:
        data, content_type = await _service().check_map(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    return Response(content=data, media_type=content_type)


# ============ Зонд, Соседи ============


@router.post('/probe', response_model=ActionOut)
async def launch_probe(
    body: ScanCreate,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionOut:
    try:
        action = await _service().launch_probe(db, admin_id=admin.id, **body.model_dump())
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'dpichecker_probe_launch', action.id, {'source': body.source})
    return ActionOut.from_action(action)


@router.post('/noisy', response_model=ActionOut)
async def launch_noisy(
    body: ScanCreate,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionOut:
    try:
        action = await _service().launch_noisy(db, admin_id=admin.id, **body.model_dump())
    except Exception as exc:
        raise _http(exc) from exc
    return ActionOut.from_action(action)


@router.get('/scans/{action_id}', response_model=ScanResponse)
async def get_scan(
    action_id: int,
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ScanResponse:
    try:
        view = await _service().get_scan(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    return ScanResponse(action=ActionOut.from_action(view['action']), scan=view['scan'])


@router.get('/scans/{action_id}/noisy.csv')
async def noisy_csv(
    action_id: int,
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    try:
        data, content_type = await _service().noisy_csv(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    return _file(data, content_type, _download_name('noisy', action_id))


# ============ Скачивание CSV в Mini App ============


@router.post('/files/{kind}/{action_id}/link', response_model=DownloadLinkOut)
async def download_link(
    kind: DownloadKind,
    action_id: int,
    request: Request,
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> DownloadLinkOut:
    """Короткая подписанная ссылка на CSV: в Mini App `<a download>` выкидывает из приложения,
    а штатный downloadFile качает URL без заголовка авторизации."""
    try:
        await _service()._action(db, action_id, 'check' if kind == 'report' else 'noisy')
    except Exception as exc:
        raise _http(exc) from exc
    token = make_media_token(f'dpichecker:{kind}:{action_id}', ttl_seconds=DOWNLOAD_TTL_SECONDS)
    url = str(request.url_for('dpichecker_signed_download', kind=kind, action_id=action_id))
    return DownloadLinkOut(url=f'{url}?token={token}', file_name=_download_name(kind, action_id))


@download_router.get('/files/{kind}/{action_id}', name='dpichecker_signed_download')
async def signed_download(
    kind: DownloadKind,
    action_id: int,
    token: str = Query(''),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    if not _verify_media_token(f'dpichecker:{kind}:{action_id}', token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Файл не найден')
    try:
        method = _service().report_csv if kind == 'report' else _service().noisy_csv
        data, content_type = await method(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    # Веб-клиент Telegram качает файл fetch-ом со своего origin — без этого заголовка скачивание молча падает.
    return _file(
        data,
        content_type,
        _download_name(kind, action_id),
        headers={'Access-Control-Allow-Origin': TELEGRAM_WEB_ORIGIN},
    )


# ============ Бесплатные справки ============


@router.get('/cheremsha')
async def cheremsha(
    resource: str = Query(min_length=1, max_length=4000, description='до 20 доменов/IP через запятую'),
    admin: User = Depends(require_permission('dpichecker:read')),
) -> dict:
    items = [item.strip() for item in resource.replace('\n', ',').split(',') if item.strip()][:CHEREMSHA_MAX]
    try:
        return await _service().cheremsha(items)
    except Exception as exc:
        raise _http(exc) from exc


@router.get('/ip/{ip}')
async def ip_lookup(
    ip: str, bgp: bool = Query(default=False), admin: User = Depends(require_permission('dpichecker:read'))
) -> dict:
    try:
        address = str(ipaddress.ip_address(ip.strip()))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Нужен IP-адрес') from exc
    try:
        return await _service().ip_lookup(address, bgp=bgp)
    except Exception as exc:
        raise _http(exc) from exc


@router.get('/blacklist')
async def blacklist(
    resource: str = Query(min_length=1, max_length=255), admin: User = Depends(require_permission('dpichecker:read'))
) -> dict:
    try:
        return await _service().blacklist(resource)
    except Exception as exc:
        raise _http(exc) from exc


# ============ Мониторы ============


@router.get('/monitors', response_model=MonitorListResponse)
async def list_monitors(
    admin: User = Depends(require_permission('dpichecker:read')), db: AsyncSession = Depends(get_cabinet_db)
) -> MonitorListResponse:
    try:
        return MonitorListResponse(items=await _service().list_monitors(db))
    except Exception as exc:
        raise _http(exc) from exc


@router.post('/monitors', response_model=ActionOut)
async def create_monitor(
    body: MonitorCreate,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionOut:
    fields = body.model_dump()
    fields['targets'] = [target.model_dump() for target in body.targets]
    try:
        action = await _service().create_monitor(db, admin_id=admin.id, **fields)
    except Exception as exc:
        raise _http(exc) from exc
    details = {'check_type': body.check_type, 'interval_hours': body.interval_hours, 'pops': len(body.pop_ids)}
    await _audit(db, admin, 'dpichecker_monitor_create', action.id, details)
    return ActionOut.from_action(action)


@router.post('/monitors/remote/{remote_id}/adopt', response_model=ActionOut)
async def adopt_monitor(
    remote_id: int,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ActionOut:
    """Монитор с сайта DPI//CHECKER — под управление кабинета (дальше им правят как своим)."""
    try:
        action = await _service().adopt_monitor(db, remote_id, admin_id=admin.id)
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'dpichecker_monitor_adopt', action.id, {'remote_id': remote_id})
    return ActionOut.from_action(action)


@router.patch('/monitors/{action_id}')
async def patch_monitor(
    action_id: int,
    body: MonitorPatch,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    patch = body.model_dump(exclude_none=True)
    try:
        monitor = await _service().update_monitor(db, action_id, patch)
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'dpichecker_monitor_update', action_id, patch)
    return monitor


@router.delete('/monitors/{action_id}')
async def delete_monitor(
    action_id: int,
    admin: User = Depends(require_permission('dpichecker:run')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    try:
        result = await _service().delete_monitor(db, action_id)
    except Exception as exc:
        raise _http(exc) from exc
    await _audit(db, admin, 'dpichecker_monitor_delete', action_id, {})
    return result


@router.get('/monitors/{action_id}/runs')
async def monitor_runs(
    action_id: int,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(require_permission('dpichecker:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    try:
        return await _service().monitor_runs(db, action_id, limit=limit, offset=offset)
    except Exception as exc:
        raise _http(exc) from exc
