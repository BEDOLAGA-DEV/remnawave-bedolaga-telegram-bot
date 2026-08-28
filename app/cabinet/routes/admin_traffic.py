"""Admin routes for traffic usage statistics."""

import asyncio
import csv
import io
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot_factory import create_bot
from app.database.models import Subscription, Transaction, TransactionType, User
from app.services.remnawave_service import RemnaWaveService

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.traffic import (
    ExportCsvRequest,
    ExportCsvResponse,
    SubscriptionEnrichmentInfo,
    SubscriptionTrafficInfo,
    TrafficEnrichmentResponse,
    TrafficNodeInfo,
    TrafficUsageResponse,
    UserTrafficEnrichment,
    UserTrafficItem,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/traffic', tags=['Admin Traffic'])

_ALLOWED_PERIODS = frozenset({1, 3, 7, 14, 30})

# In-memory cache: {(start_str, end_str): (timestamp, aggregated_data, nodes_info)}
_traffic_cache: dict[tuple[str, str], tuple[float, dict[int, dict[str, int]], list[TrafficNodeInfo]]] = {}
_CACHE_TTL = 300  # 5 minutes
_cache_lock = asyncio.Lock()

# Valid sort fields for the GET endpoint
_SORT_FIELDS = frozenset({'total_bytes', 'full_name', 'tariff_name', 'device_limit', 'traffic_limit_gb'})
_ENRICHMENT_SORT_FIELDS = frozenset({'connected', 'total_spent', 'sub_start', 'sub_end', 'last_node'})


def _get_status(sub) -> str | None:
    """Get subscription status via actual_status property."""
    return sub.actual_status


def _validate_period(period: int) -> None:
    if period not in _ALLOWED_PERIODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Period must be one of: {sorted(_ALLOWED_PERIODS)}',
        )


async def _aggregate_traffic(
    start_str: str, end_str: str, panel_user_ids: list[int]
) -> tuple[dict[int, dict[str, int]], list[TrafficNodeInfo]]:
    """Aggregate per-user traffic across all nodes for a given date range.

    Remnawave 3.0.0 удалил legacy-эндпоинт с построчной разбивкой
    ({userUuid, nodeUuid, total}), а его нелегаси-замена
    ``GET /api/bandwidth-stats/nodes/{uuid}/users`` отдаёт ``topUsers`` вообще
    без идентификатора пользователя — для таблицы «пользователь × узел» она
    непригодна. Поэтому берём ``POST /api/bandwidth-stats/nodes/usage``:
    ``{nodes: [{uuid, users: [{id, totalBytes}]}]}`` — один запрос на все узлы
    вместо O(nodes), ценой потери разбивки по дням (страница её и не показывала).

    Returns (user_traffic, nodes_info) where:
      user_traffic = {remnawave_id: {node_uuid: total_bytes, ...}}
      nodes_info = [TrafficNodeInfo, ...]
    """
    cache_key = (start_str, end_str)

    # Quick check without lock
    now = time.time()
    cached = _traffic_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1], cached[2]

    # Acquire lock for the slow path
    async with _cache_lock:
        # Re-check after acquiring lock
        now = time.time()
        cached = _traffic_cache.get(cache_key)
        if cached and (now - cached[0]) < _CACHE_TTL:
            return cached[1], cached[2]

        service = RemnaWaveService()
        if not service.is_configured:
            return {}, []

        known_panel_user_ids = set(panel_user_ids)

        async with service.get_api_client() as api:
            try:
                nodes = await api.get_all_nodes()
            except Exception:
                logger.warning('Failed to fetch nodes for traffic aggregation', exc_info=True)
                # Не кэшируем: `_cache_lock` и так сериализует медленный путь в
                # один запрос к панели, поэтому «завалить» её нельзя, а вот
                # запомнить пустой результат — значит показывать всем админам и
                # CSV-экспорту пустую таблицу ещё пять минут после того, как
                # панель уже поднялась. Тот же довод, что и у сбоя usage ниже.
                return {}, []

            usage: dict[str, Any] = {}
            usage_failed = False
            node_uuids = [node.uuid for node in nodes]
            if node_uuids:
                try:
                    # Эндпоинт принимает даты только как YYYY-MM-DD; ISO с `Z`
                    # (в котором строятся ключи кэша) панель отвергает с 400.
                    usage = await api.get_bandwidth_stats_nodes_usage(node_uuids, start_str[:10], end_str[:10])
                except Exception:
                    logger.warning('Failed to get per-user traffic for nodes', exc_info=True)
                    usage = {}
                    usage_failed = True

        nodes_info: list[TrafficNodeInfo] = [
            TrafficNodeInfo(node_uuid=node.uuid, node_name=node.name, country_code=node.country_code) for node in nodes
        ]
        nodes_info.sort(key=lambda n: n.node_name)

        # Response: {nodes: [{uuid, users: [{id, totalBytes}, ...]}, ...]}
        user_traffic: dict[int, dict[str, int]] = {}
        for node_entry in usage.get('nodes') or []:
            node_uuid = node_entry.get('uuid')
            if not node_uuid:
                continue
            for user_entry in node_entry.get('users') or []:
                try:
                    panel_user_id = int(user_entry['id'])
                    total = int(user_entry.get('totalBytes') or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if total > 0 and panel_user_id in known_panel_user_ids:
                    per_node = user_traffic.setdefault(panel_user_id, {})
                    per_node[node_uuid] = per_node.get(node_uuid, 0) + total

        # Раньше трафик собирался по одной ноде за раз, и сбой одной ноды терял
        # только её колонку. Теперь это один запрос на все ноды, поэтому его
        # провал обнуляет ВСЮ таблицу — и закэшировать такой результат значит
        # показывать всем админам (и отдавать в CSV-экспорт) нули следующие
        # пять минут. Отдаём пустой результат, но не запоминаем его.
        if not usage_failed:
            _traffic_cache[cache_key] = (now, user_traffic, nodes_info)

        # Evict expired entries to prevent unbounded growth
        expired = [k for k, (ts, _, _) in _traffic_cache.items() if (now - ts) >= _CACHE_TTL]
        for k in expired:
            del _traffic_cache[k]

        return user_traffic, nodes_info


def _compute_date_range(period_days: int) -> tuple[str, str]:
    """Compute ISO date-time range from period days.

    Truncates to 5-minute intervals for stable cache keys.
    """
    end_dt = datetime.now(UTC).replace(second=0, microsecond=0)
    end_dt = end_dt.replace(minute=(end_dt.minute // 5) * 5)
    start_dt = end_dt - timedelta(days=period_days)
    return start_dt.strftime('%Y-%m-%dT%H:%M:%SZ'), end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')


async def _load_user_map(db: AsyncSession) -> dict[int, User]:
    """Load all users with remnawave_id, eagerly loading subscription + tariff.

    In multi-tariff mode panel ids live on Subscription rows, not on User.
    Both sources are merged so the caller gets a complete panel id → User map.
    """
    from app.config import settings

    # Build user map from both user-level and subscription-level panel ids
    user_map: dict[int, User] = {}

    # Legacy: user-level panel ids
    stmt_users = (
        select(User)
        .where(User.remnawave_id.isnot(None))
        .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
    )
    result_users = await db.execute(stmt_users)
    users = result_users.scalars().all()
    for u in users:
        if u.remnawave_id:
            user_map[u.remnawave_id] = u

    # Multi-tariff: subscription-level panel ids
    if settings.is_multi_tariff_enabled():
        stmt_subs = (
            select(Subscription)
            .where(Subscription.remnawave_id.isnot(None))
            .options(selectinload(Subscription.user).selectinload(User.subscriptions).selectinload(Subscription.tariff))
        )
        result_subs = await db.execute(stmt_subs)
        subs = result_subs.scalars().all()
        for sub in subs:
            if sub.remnawave_id and sub.user and sub.remnawave_id not in user_map:
                user_map[sub.remnawave_id] = sub.user

    return user_map


def _build_traffic_items(
    user_traffic: dict[int, dict[str, int]],
    user_map: dict[int, User],
    nodes_info: list[TrafficNodeInfo],
    search: str = '',
    sort_by: str = 'total_bytes',
    sort_desc: bool = True,
    tariff_filter: set[str] | None = None,
    status_filter: set[str] | None = None,
    node_filter: set[str] | None = None,
) -> list[UserTrafficItem]:
    """Merge traffic data with user data, apply search/tariff/status/node filters, return sorted list."""
    items: list[UserTrafficItem] = []
    search_lower = search.lower().strip()

    all_panel_user_ids = set(user_traffic.keys()) | set(user_map.keys())
    for panel_user_id in all_panel_user_ids:
        user = user_map.get(panel_user_id)
        if not user:
            continue

        traffic = user_traffic.get(panel_user_id, {})

        full_name = user.full_name
        username = user.username
        email = user.email

        if search_lower:
            if (
                search_lower not in (full_name or '').lower()
                and search_lower not in (username or '').lower()
                and search_lower not in (email or '').lower()
            ):
                continue

        subs = getattr(user, 'subscriptions', None) or []

        # Primary subscription for backward-compat top-level fields
        primary_sub = next((s for s in subs if s.is_active), subs[0] if subs else None)
        tariff_name = None
        subscription_status = None
        traffic_limit_gb = 0.0
        device_limit = 1

        if primary_sub:
            subscription_status = _get_status(primary_sub)
            traffic_limit_gb = float(primary_sub.traffic_limit_gb or 0)
            device_limit = primary_sub.device_limit or 1
            if primary_sub.tariff:
                tariff_name = primary_sub.tariff.name

        # Filtering uses primary sub values (keeps existing filter semantics)
        if tariff_filter is not None:
            if (tariff_name or '') not in tariff_filter:
                continue

        if status_filter is not None:
            if (subscription_status or '') not in status_filter:
                continue

        # Apply node filter: keep only selected nodes, recalculate total
        if node_filter is not None:
            traffic = {k: v for k, v in traffic.items() if k in node_filter}

        total_bytes = sum(traffic.values())

        # Build per-subscription detail list for multi-subscription display
        subscriptions_traffic = [
            SubscriptionTrafficInfo(
                subscription_id=sub.id,
                tariff_name=sub.tariff.name if sub.tariff else None,
                status=_get_status(sub),
                traffic_limit_gb=float(sub.traffic_limit_gb or 0),
                device_limit=sub.device_limit or 1,
            )
            for sub in subs
        ]

        items.append(
            UserTrafficItem(
                user_id=user.id,
                telegram_id=user.telegram_id,
                username=username,
                email=email,
                full_name=full_name,
                tariff_name=tariff_name,
                subscription_status=subscription_status,
                traffic_limit_gb=traffic_limit_gb,
                device_limit=device_limit,
                node_traffic=traffic,
                total_bytes=total_bytes,
                subscriptions=subscriptions_traffic,
            )
        )

    # Sort by the requested field; node columns use 'node_<uuid>' prefix
    if sort_by.startswith('node_'):
        node_uuid = sort_by[5:]
        items.sort(key=lambda x: x.node_traffic.get(node_uuid, 0), reverse=sort_desc)
    elif sort_by in ('full_name', 'tariff_name'):
        items.sort(key=lambda x: (getattr(x, sort_by, None) or '').lower(), reverse=sort_desc)
    else:
        items.sort(key=lambda x: getattr(x, sort_by, 0) or 0, reverse=sort_desc)

    return items


@router.get('', response_model=TrafficUsageResponse)
async def get_traffic_usage(
    admin: User = Depends(require_permission('traffic:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    period: int = Query(30, ge=1, le=30),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query('', max_length=100),
    sort_by: str = Query('total_bytes', max_length=100),
    sort_desc: bool = Query(True),
    tariffs: str = Query('', max_length=500),
    statuses: str = Query('', max_length=500),
    nodes: str = Query('', max_length=2000),
    start_date: str = Query('', max_length=10),
    end_date: str = Query('', max_length=10),
):
    """Get paginated per-user traffic usage by node."""
    # Determine date range: custom dates or period-based
    if start_date.strip() and end_date.strip():
        try:
            start_dt = datetime.strptime(start_date.strip(), '%Y-%m-%d').replace(tzinfo=UTC)
            end_dt = datetime.strptime(end_date.strip(), '%Y-%m-%d').replace(tzinfo=UTC, hour=23, minute=59, second=59)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid date format. Use YYYY-MM-DD.')

        now = datetime.now(UTC)
        end_dt = min(end_dt, now)

        if start_dt > end_dt:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='start_date must be before end_date.')

        if (end_dt - start_dt).days > 31:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Date range cannot exceed 31 days.')

        start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        effective_period = (end_dt - start_dt).days or 1
    else:
        _validate_period(period)
        start_str, end_str = _compute_date_range(period)
        effective_period = period

    user_map = await _load_user_map(db)
    user_traffic, nodes_info = await _aggregate_traffic(start_str, end_str, list(user_map.keys()))

    # Collect all available tariff names (before filtering)
    available_tariffs = sorted(
        {
            sub.tariff.name
            for u in user_map.values()
            for sub in (getattr(u, 'subscriptions', None) or [])
            if sub.tariff and sub.tariff.name
        }
    )

    # Collect all available statuses (before filtering)
    available_statuses = sorted(
        {
            _get_status(sub)
            for u in user_map.values()
            for sub in (getattr(u, 'subscriptions', None) or [])
            if _get_status(sub)
        }
    )

    # Parse tariff filter
    tariff_filter: set[str] | None = None
    if tariffs.strip():
        tariff_filter = {t.strip() for t in tariffs.split(',') if t.strip()}

    # Parse status filter
    status_filter: set[str] | None = None
    if statuses.strip():
        status_filter = {s.strip() for s in statuses.split(',') if s.strip()}

    # Parse node filter
    node_filter: set[str] | None = None
    all_node_uuids = {n.node_uuid for n in nodes_info}
    if nodes.strip():
        node_filter = {n.strip() for n in nodes.split(',') if n.strip()} & all_node_uuids
        if not node_filter:
            node_filter = None  # No valid nodes matched, treat as "all nodes"

    # Validate sort_by: allow known fields + enrichment fields + 'node_<uuid>'
    is_node_sort = sort_by.startswith('node_') and sort_by[5:] in all_node_uuids
    is_enrichment_sort = sort_by in _ENRICHMENT_SORT_FIELDS
    if sort_by not in _SORT_FIELDS and not is_node_sort and not is_enrichment_sort:
        sort_by = 'total_bytes'

    # For enrichment sort, build items unsorted then sort by enrichment field
    effective_sort = 'total_bytes' if is_enrichment_sort else sort_by
    items = _build_traffic_items(
        user_traffic, user_map, nodes_info, search, effective_sort, sort_desc, tariff_filter, status_filter, node_filter
    )

    if is_enrichment_sort:
        enrichment_data, _ = await _build_enrichment(db, user_map)
        enr_key_map = {
            'connected': lambda e: e.devices_connected,
            'total_spent': lambda e: e.total_spent_kopeks,
            'sub_start': lambda e: e.subscription_start_date or '',
            'sub_end': lambda e: e.subscription_end_date or '',
            'last_node': lambda e: e.last_node_name or '',
        }
        key_fn = enr_key_map[sort_by]
        empty = UserTrafficEnrichment()
        items.sort(key=lambda x: key_fn(enrichment_data.get(x.user_id, empty)), reverse=sort_desc)

    total = len(items)
    paginated = items[offset : offset + limit]

    return TrafficUsageResponse(
        items=paginated,
        nodes=nodes_info,
        total=total,
        offset=offset,
        limit=limit,
        period_days=effective_period,
        available_tariffs=available_tariffs,
        available_statuses=available_statuses,
    )


# ============== Enrichment endpoint ==============

_enrichment_cache: dict[str, tuple[float, dict[int, UserTrafficEnrichment]]] = {}
_ENRICHMENT_CACHE_TTL = 300  # 5 minutes
_enrichment_lock = asyncio.Lock()


async def _get_bulk_spending(db: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    """Get total spent kopeks for multiple users in a single query."""
    if not user_ids:
        return {}
    result = await db.execute(
        select(Transaction.user_id, func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0))
        .where(
            and_(
                Transaction.user_id.in_(user_ids),
                Transaction.is_completed.is_(True),
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            )
        )
        .group_by(Transaction.user_id)
    )
    return {row[0]: int(row[1]) for row in result.all()}


async def _build_enrichment(
    db: AsyncSession, user_map: dict[int, User]
) -> tuple[dict[int, UserTrafficEnrichment], bool]:
    """Build enrichment data for all users: devices, spending, dates, last node.

    Второй элемент — признак того, что панельные данные получить не удалось.
    Без него результат с обнулёнными «устройствами» и «последней нодой» попадал
    в общий 5-минутный кэш и продолжал показываться всем админам и CSV-экспорту
    ещё долго после того, как панель поднялась.
    """
    panel_degraded = False
    panel_user_id_to_user_id: dict[int, int] = {}
    for panel_user_id, user in user_map.items():
        panel_user_id_to_user_id[panel_user_id] = user.id

    service = RemnaWaveService()
    devices_by_user: dict[int, int] = {}
    last_node_uuid_by_user: dict[int, str] = {}
    node_uuid_to_name: dict[str, str] = {}

    if service.is_configured:
        async with service.get_api_client() as api:
            # 3 bulk calls: nodes + users (paginated) + devices
            try:
                nodes_list = await api.get_all_nodes()
            except Exception:
                logger.warning('Failed to fetch nodes for enrichment', exc_info=True)
                nodes_list = []
                panel_degraded = True

            for node in nodes_list:
                node_uuid_to_name[node.uuid] = node.name

            # Fetch all panel users (paginated) for last connected node
            panel_users = []
            try:
                first_page = await api.get_all_users(start=0, size=500)
                panel_users.extend(first_page['users'])
                total_panel = first_page['total']

                if total_panel > 500:
                    remaining_tasks = [
                        api.get_all_users(start=offset, size=500) for offset in range(500, total_panel, 500)
                    ]
                    pages = await asyncio.gather(*remaining_tasks, return_exceptions=True)
                    for page in pages:
                        if isinstance(page, dict):
                            panel_users.extend(page['users'])
            except Exception:
                logger.warning('Failed to fetch panel users for enrichment', exc_info=True)
                panel_degraded = True

            # HWID-устройства ссылаются на пользователя по числовому panel id
            # (device['userId']) — тем же, которым панельная запись теперь и
            # идентифицируется, поэтому отдельная карта больше не нужна: берём
            # только тех панельных юзеров, которых знаем локально.
            for pu in panel_users:
                uid = panel_user_id_to_user_id.get(pu.id)
                if uid is None:
                    continue
                if pu.user_traffic and pu.user_traffic.last_connected_node_uuid:
                    last_node_uuid_by_user[uid] = pu.user_traffic.last_connected_node_uuid

            # Bulk device fetch — single API call (paginated with start/size)
            try:
                devices_data = await api.get_all_hwid_devices()
                for device in devices_data.get('devices', []):
                    uid = panel_user_id_to_user_id.get(device.get('userId'))
                    if uid is not None:
                        devices_by_user[uid] = devices_by_user.get(uid, 0) + 1
            except Exception:
                logger.warning('Failed to fetch bulk devices for enrichment', exc_info=True)
                panel_degraded = True

    # Bulk spending stats
    all_user_ids = [u.id for u in user_map.values()]
    spending_map = await _get_bulk_spending(db, all_user_ids)

    # Build enrichment data
    enrichment: dict[int, UserTrafficEnrichment] = {}
    for user in user_map.values():
        uid = user.id
        subs_list = getattr(user, 'subscriptions', None) or []

        # Primary subscription for backward-compat top-level date fields
        primary_sub = next((s for s in subs_list if s.is_active), subs_list[0] if subs_list else None)

        start_date = None
        end_date = None
        if primary_sub:
            if primary_sub.start_date:
                start_date = primary_sub.start_date.isoformat()
            if primary_sub.end_date:
                end_date = primary_sub.end_date.isoformat()

        last_node_name = None
        last_uuid = last_node_uuid_by_user.get(uid)
        if last_uuid:
            last_node_name = node_uuid_to_name.get(last_uuid)

        # Build per-subscription enrichment list for multi-subscription display
        subscriptions_enrichment = [
            SubscriptionEnrichmentInfo(
                subscription_id=sub.id,
                tariff_name=sub.tariff.name if sub.tariff else None,
                start_date=sub.start_date.isoformat() if sub.start_date else None,
                end_date=sub.end_date.isoformat() if sub.end_date else None,
            )
            for sub in subs_list
        ]

        enrichment[uid] = UserTrafficEnrichment(
            devices_connected=devices_by_user.get(uid, 0),
            total_spent_kopeks=spending_map.get(uid, 0),
            subscription_start_date=start_date,
            subscription_end_date=end_date,
            last_node_name=last_node_name,
            subscriptions=subscriptions_enrichment,
        )

    return enrichment, panel_degraded


@router.get('/enrichment', response_model=TrafficEnrichmentResponse)
async def get_traffic_enrichment(
    admin: User = Depends(require_permission('traffic:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Return enrichment data: device counts, spending, dates, last node."""
    cache_key = 'enrichment'
    now = time.time()

    cached = _enrichment_cache.get(cache_key)
    if cached and (now - cached[0]) < _ENRICHMENT_CACHE_TTL:
        return TrafficEnrichmentResponse(data=cached[1])

    async with _enrichment_lock:
        now = time.time()
        cached = _enrichment_cache.get(cache_key)
        if cached and (now - cached[0]) < _ENRICHMENT_CACHE_TTL:
            return TrafficEnrichmentResponse(data=cached[1])

        user_map = await _load_user_map(db)
        enrichment, panel_degraded = await _build_enrichment(db, user_map)

        # Тот же довод, что и у _aggregate_traffic: запомнить деградированный
        # ответ значит показывать нули ещё пять минут после восстановления.
        if not panel_degraded:
            _enrichment_cache[cache_key] = (now, enrichment)

        # Evict expired
        expired = [k for k, (ts, _) in _enrichment_cache.items() if (now - ts) >= _ENRICHMENT_CACHE_TTL]
        for k in expired:
            del _enrichment_cache[k]

        return TrafficEnrichmentResponse(data=enrichment)


@router.post('/export-csv', response_model=ExportCsvResponse)
async def export_traffic_csv(
    request: ExportCsvRequest,
    admin: User = Depends(require_permission('traffic:export')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Generate CSV with traffic usage and send to admin's Telegram DM."""
    if not admin.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Admin has no Telegram ID configured',
        )

    # Determine date range: custom dates or period-based
    if request.start_date and request.end_date:
        try:
            start_dt = datetime.strptime(request.start_date.strip(), '%Y-%m-%d').replace(tzinfo=UTC)
            end_dt = datetime.strptime(request.end_date.strip(), '%Y-%m-%d').replace(
                tzinfo=UTC, hour=23, minute=59, second=59
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid date format. Use YYYY-MM-DD.')

        now = datetime.now(UTC)
        end_dt = min(end_dt, now)
        if start_dt > end_dt:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='start_date must be before end_date.')
        if (end_dt - start_dt).days > 31:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Date range cannot exceed 31 days.')

        start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        period_label = f'{request.start_date}_{request.end_date}'
        # Длину периода считаем здесь же, где разобрали даты. Раньше она
        # вычислялась ниже под ПОВТОРНЫМ `if request.start_date and
        # request.end_date`, то есть start_dt/end_dt читались за 60 строк от
        # места присваивания под скопированным условием: разойдись эти два
        # условия — UnboundLocalError и 500 на выгрузке CSV.
        period_days = max((end_dt - start_dt).days, 1)
    else:
        _validate_period(request.period)
        start_str, end_str = _compute_date_range(request.period)
        period_label = f'{request.period}d'
        period_days = request.period

    user_map = await _load_user_map(db)
    user_traffic, nodes_info = await _aggregate_traffic(start_str, end_str, list(user_map.keys()))
    enrichment, _ = await _build_enrichment(db, user_map)

    # Parse filters
    tariff_filter: set[str] | None = None
    if request.tariffs and request.tariffs.strip():
        tariff_filter = {t.strip() for t in request.tariffs.split(',') if t.strip()}

    status_filter: set[str] | None = None
    if request.statuses and request.statuses.strip():
        status_filter = {s.strip() for s in request.statuses.split(',') if s.strip()}

    node_filter: set[str] | None = None
    all_node_uuids = {n.node_uuid for n in nodes_info}
    if request.nodes and request.nodes.strip():
        node_filter = {n.strip() for n in request.nodes.split(',') if n.strip()} & all_node_uuids
        if not node_filter:
            node_filter = None

    items = _build_traffic_items(
        user_traffic,
        user_map,
        nodes_info,
        sort_by='total_bytes',
        sort_desc=True,
        tariff_filter=tariff_filter,
        status_filter=status_filter,
        node_filter=node_filter,
    )

    # Determine which nodes to include in CSV columns
    csv_nodes = [n for n in nodes_info if n.node_uuid in node_filter] if node_filter else nodes_info

    total_thr = request.total_threshold_gb or 0
    node_thr = request.node_threshold_gb or 0
    has_risk = total_thr > 0 or node_thr > 0

    # Build CSV rows
    rows: list[dict] = []
    for item in items:
        row: dict = {
            'User ID': item.user_id,
            'Telegram ID': item.telegram_id or '',
            'Username': item.username or '',
            'Email': item.email or '',
            'Full Name': item.full_name,
            'Tariff': item.tariff_name or '',
            'Status': item.subscription_status or '',
            'Traffic Limit (GB)': item.traffic_limit_gb,
            'Device Limit': item.device_limit,
        }
        # Enrichment columns
        enr = enrichment.get(item.user_id)
        row['Connected Devices'] = enr.devices_connected if enr else 0
        row['Total Spent (RUB)'] = round(enr.total_spent_kopeks / 100, 2) if enr else 0
        row['Sub Start'] = enr.subscription_start_date or '' if enr else ''
        row['Sub End'] = enr.subscription_end_date or '' if enr else ''
        row['Last Node'] = enr.last_node_name or '' if enr else ''

        for node in csv_nodes:
            row[f'{node.node_name} (bytes)'] = item.node_traffic.get(node.node_uuid, 0)
        row['Total (bytes)'] = item.total_bytes
        row['Total (GB)'] = round(item.total_bytes / (1024**3), 2) if item.total_bytes else 0

        if has_risk:
            daily_total = item.total_bytes / period_days / (1024**3) if period_days > 0 else 0
            row['Total GB/day'] = round(daily_total, 4)

            total_ratio = daily_total / total_thr if total_thr > 0 else 0

            max_node_ratio = 0.0
            worst_node_daily = 0.0
            for node_bytes in item.node_traffic.values():
                if node_bytes > 0 and node_thr > 0:
                    daily_node = node_bytes / period_days / (1024**3) if period_days > 0 else 0
                    ratio = daily_node / node_thr
                    if ratio > max_node_ratio:
                        max_node_ratio = ratio
                        worst_node_daily = daily_node

            ratio = max(total_ratio, max_node_ratio)
            if ratio < 0.5:
                risk_level = 'low'
            elif ratio < 0.8:
                risk_level = 'medium'
            elif ratio < 1.2:
                risk_level = 'high'
            else:
                risk_level = 'critical'

            row['Risk Level'] = risk_level
            row['Risk Ratio'] = round(ratio, 3)
            row['Risk GB/day'] = round(daily_total if total_ratio >= max_node_ratio else worst_node_daily, 4)

        rows.append(row)

    # Generate CSV
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    csv_bytes = output.getvalue().encode('utf-8-sig')

    timestamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
    filename = f'traffic_usage_{period_label}_{timestamp}.csv'

    try:
        bot = create_bot()
        async with bot:
            await bot.send_document(
                chat_id=admin.telegram_id,
                document=BufferedInputFile(csv_bytes, filename=filename),
                caption=f'Traffic usage report ({period_label})\nUsers: {len(rows)}',
            )
    except Exception:
        logger.error('Failed to send CSV to admin', telegram_id=admin.telegram_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to send CSV report. Please try again later.',
        )

    return ExportCsvResponse(success=True, message=f'CSV sent ({len(rows)} users)')
