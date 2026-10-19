"""Локации в админке кабинета: приём отчётов счётчиков с нод и выдача карточек.

Счётчики на нодах раз в минуту присылают, какие устройства они видели за последние
пять минут (`POST /report`, авторизация — общий токен в заголовке). Отчёт живёт в
Redis три минуты; `GET /` сводит отчёты всех нод с хостами и нодами панели.
"""

import hmac
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.services.location_presence import (
    WINDOW,
    LocationCard,
    build_location_cards,
    devices_from_report,
    merge_presence,
)
from app.services.remnawave_service import RemnaWaveService
from app.utils.cache import cache

from ..dependencies import require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/stats/locations', tags=['Cabinet Admin Locations'])

REPORT_KEY_PREFIX = 'location_report:'
REPORT_TTL_SECONDS = 180
MAX_DEVICES = 5000


# ============ Schemas ============


class ReportDevice(BaseModel):
    u: str = Field(min_length=1, max_length=64)
    d: str = Field(min_length=1, max_length=64)
    t: datetime
    inbound: str = Field(min_length=1, max_length=128)
    exit: str | None = Field(default=None, max_length=128)


class LocationReport(BaseModel):
    node: str = Field(min_length=1, max_length=64)
    generated_at: datetime
    window_seconds: int = Field(default=int(WINDOW.total_seconds()), ge=1, le=3600)
    devices: list[ReportDevice] = Field(max_length=MAX_DEVICES)


class ReportAccepted(BaseModel):
    ok: bool = True


class LocationExit(BaseModel):
    name: str
    users: int


class LocationItem(BaseModel):
    title: str
    node_name: str | None = None
    inbound_tag: str | None = None
    users: int | None = None
    exits: list[LocationExit] = Field(default_factory=list)
    source: str | None = None
    reported_at: datetime | None = None


class LocationsOverview(BaseModel):
    # False — LOCATION_COUNTER_TOKEN не задан: счётчики не настроены, кабинет блок не показывает.
    enabled: bool = True
    window_seconds: int
    generated_at: datetime
    locations: list[LocationItem]


# ============ Helpers ============


async def require_counter_token(
    x_location_counter_token: Annotated[str | None, Header()] = None,
) -> None:
    """Пустой токен в настройках выключает приём; иначе заголовок должен совпасть с ним."""
    expected = settings.LOCATION_COUNTER_TOKEN
    if not expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')
    if not hmac.compare_digest((x_location_counter_token or '').encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid token')


def _card_to_item(card: LocationCard) -> LocationItem:
    return LocationItem(
        title=card.title,
        node_name=card.node_name,
        inbound_tag=card.inbound_tag,
        users=card.users,
        exits=[LocationExit(name=name, users=users) for name, users in card.exits],
        source=card.source,
        reported_at=card.reported_at,
    )


async def _load_reports() -> list[dict]:
    reports: list[dict] = []
    for key in await cache.get_keys(f'{REPORT_KEY_PREFIX}*'):
        report = await cache.get(key)
        if isinstance(report, dict):
            reports.append(report)
    return reports


# ============ Endpoints ============


@router.post('/report', response_model=ReportAccepted, dependencies=[Depends(require_counter_token)])
async def receive_location_report(report: LocationReport) -> ReportAccepted:
    """Принять отчёт счётчика с ноды (без админской авторизации — только токен)."""
    stored = await cache.set(
        f'{REPORT_KEY_PREFIX}{report.node}',
        report.model_dump(mode='json'),
        expire=REPORT_TTL_SECONDS,
    )
    if not stored:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Failed to store report')
    return ReportAccepted()


@router.get('', response_model=LocationsOverview)
async def get_locations(
    admin=Depends(require_permission('stats:read')),
) -> LocationsOverview:
    """Карточки локаций: хосты панели, на каждой — число пользователей онлайн."""
    now = datetime.now(UTC)
    if not settings.LOCATION_COUNTER_TOKEN:
        return LocationsOverview(
            enabled=False,
            window_seconds=int(WINDOW.total_seconds()),
            generated_at=now,
            locations=[],
        )

    devices = []
    reports_at: dict[str, datetime] = {}
    for report in await _load_reports():
        try:
            node_devices = devices_from_report(report)
            generated_at = datetime.fromisoformat(str(report['generated_at']).replace('Z', '+00:00'))
            node = str(report['node'])
        except (KeyError, TypeError, ValueError):
            logger.warning('Пропущен повреждённый отчёт счётчика локаций')
            continue
        devices.extend(node_devices)
        reports_at[node] = generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=UTC)

    try:
        async with RemnaWaveService().get_api_client() as api:
            hosts = await api.get_all_hosts()
            nodes = await api.get_all_nodes()
    except Exception as error:
        logger.error('Не удалось загрузить хосты и ноды панели для локаций', error=error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Failed to load locations',
        ) from error

    cards = build_location_cards(
        hosts,
        nodes,
        merge_presence(devices, now),
        reports_at,
        settings.get_location_host_node_overrides(),
        settings.get_location_exit_breakdown_inbounds(),
        settings.get_location_skip_host_markers(),
    )
    return LocationsOverview(
        window_seconds=int(WINDOW.total_seconds()),
        generated_at=now,
        locations=[_card_to_item(card) for card in cards],
    )
