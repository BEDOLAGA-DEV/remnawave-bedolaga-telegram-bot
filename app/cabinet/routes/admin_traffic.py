"""Admin routes for traffic usage statistics."""

import asyncio
import csv
import io
import logging
import time
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, Transaction, TransactionType, User
from app.services.remnawave_service import RemnaWaveService

from ..dependencies import get_cabinet_db, get_current_admin_user
from ..schemas.traffic import (
    ExportCsvRequest,
    ExportCsvResponse,
    TrafficEnrichmentResponse,
    TrafficNodeInfo,
    TrafficUsageResponse,
    UserTrafficEnrichment,
    UserTrafficItem,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/admin/traffic', tags=['Admin Traffic'])

_ALLOWED_PERIODS = frozenset({1, 3, 7, 14, 30})
_CONCURRENCY_LIMIT = 5  # Max parallel API calls to avoid rate limiting

# In-memory cache: {(start_str, end_str): (timestamp, aggregated_data, nodes_info)}
_traffic_cache: dict[tuple[str, str], tuple[float, dict[str, dict[str, int]], list[TrafficNodeInfo]]] = {}
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
    start_str: str, end_str: str, user_uuids: list[str]
) -> tuple[dict[str, dict[str, int]], list[TrafficNodeInfo]]:
    """Aggregate per-user traffic across all nodes for a given date range.

    Uses legacy per-node endpoint to fetch all users' traffic per node —
    O(nodes) API calls instead of O(users). The legacy endpoint returns
    {userUuid, nodeUuid, total} per entry (non-legacy only returns topUsers
    without userUuid).

    Returns (user_traffic, nodes_info) where:
      user_traffic = {remnawave_uuid: {node_uuid: total_bytes, ...}}
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

        user_uuids_set = set(user_uuids)

        async with service.get_api_client() as api:
            nodes = await api.get_all_nodes()

            # Fetch per-node user stats — O(nodes) calls instead of O(users)
            semaphore = asyncio.Semaphore(_CONCURRENCY_LIMIT)

            async def fetch_node_users(node):
                async with semaphore:
                    try:
                        stats = await api.get_bandwidth_stats_node_users_legacy(node.uuid, start_str, end_str)
                        return node.uuid, stats
                    except Exception:
                        logger.warning('Failed to get traffic for node %s', node.name, exc_info=True)
                        return node.uuid, None

            results = await asyncio.gather(*(fetch_node_users(n) for n in nodes))

        nodes_info: list[TrafficNodeInfo] = [
            TrafficNodeInfo(node_uuid=node.uuid, node_name=node.name, country_code=node.country_code) for node in nodes
        ]
        nodes_info.sort(key=lambda n: n.node_name)

        # Legacy response: [{userUuid, username, nodeUuid, total, date}, ...]
        user_traffic: dict[str, dict[str, int]] = {}
        for node_uuid, entries in results:
            if not isinstance(entries, list):
                continue
            for entry in entries:
                uid = entry.get('userUuid', '')
                total = int(entry.get('total', 0))
                if uid and total > 0 and uid in user_uuids_set:
                    user_traffic.setdefault(uid, {})[node_uuid] = user_traffic.get(uid, {}).get(node_uuid, 0) + total

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


async def _load_user_map(db: AsyncSession) -> dict[str, User]:
    """Load all users with remnawave_uuid, eagerly loading subscription + tariff."""
    stmt = (
        select(User)
        .where(User.remnawave_uuid.isnot(None))
        .options(selectinload(User.subscription).selectinload(Subscription.tariff))
    )
    result = await db.execute(stmt)
    users = result.scalars().all()
    return {u.remnawave_uuid: u for u in users if u.remnawave_uuid}


def _build_traffic_items(
    user_traffic: dict[str, dict[str, int]],
    user_map: dict[str, User],
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

    all_uuids = set(user_traffic.keys()) | set(user_map.keys())
    for uuid in all_uuids:
        user = user_map.get(uuid)
        if not user:
            continue

        traffic = user_traffic.get(uuid, {})

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

        sub = user.subscription
        tariff_name = None
        subscription_status = None
        traffic_limit_gb = 0.0
        device_limit = 1

        if sub:
            subscription_status = _get_status(sub)
            traffic_limit_gb = float(sub.traffic_limit_gb or 0)
            device_limit = sub.device_limit or 1
            if sub.tariff:
                tariff_name = sub.tariff.name

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
    admin: User = Depends(get_current_admin_user),
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
            u.subscription.tariff.name
            for u in user_map.values()
            if u.subscription and u.subscription.tariff and u.subscription.tariff.name
        }
    )

    # Collect all available statuses (before filtering)
    available_statuses = sorted(
        {_get_status(sub) for u in user_map.values() if (sub := u.subscription) and _get_status(sub)}
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
        enrichment_data = await _build_enrichment(db, user_map)
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
        select(Transaction.user_id, func.coalesce(func.sum(Transaction.amount_kopeks), 0))
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


async def _build_enrichment(db: AsyncSession, user_map: dict[str, User]) -> dict[int, UserTrafficEnrichment]:
    """Build enrichment data for all users: devices, spending, dates, last node."""
    uuid_to_user_id: dict[str, int] = {}
    for uuid, user in user_map.items():
        uuid_to_user_id[uuid] = user.id

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

            for pu in panel_users:
                uid = uuid_to_user_id.get(pu.uuid)
                if uid is None:
                    continue
                if pu.user_traffic and pu.user_traffic.last_connected_node_uuid:
                    last_node_uuid_by_user[uid] = pu.user_traffic.last_connected_node_uuid

            # Bulk device fetch — single API call (paginated with start/size)
            try:
                devices_data = await api.get_all_hwid_devices()
                for device in devices_data.get('devices', []):
                    user_uuid = device.get('userUuid', '')
                    uid = uuid_to_user_id.get(user_uuid)
                    if uid is not None:
                        devices_by_user[uid] = devices_by_user.get(uid, 0) + 1
            except Exception:
                logger.warning('Failed to fetch bulk devices for enrichment', exc_info=True)

    # Bulk spending stats
    all_user_ids = [u.id for u in user_map.values()]
    spending_map = await _get_bulk_spending(db, all_user_ids)

    # Build enrichment data
    enrichment: dict[int, UserTrafficEnrichment] = {}
    for uuid, user in user_map.items():
        uid = user.id
        sub = user.subscription

        start_date = None
        end_date = None
        if sub:
            if sub.start_date:
                start_date = sub.start_date.isoformat()
            if sub.end_date:
                end_date = sub.end_date.isoformat()

        last_node_name = None
        last_uuid = last_node_uuid_by_user.get(uid)
        if last_uuid:
            last_node_name = node_uuid_to_name.get(last_uuid)

        enrichment[uid] = UserTrafficEnrichment(
            devices_connected=devices_by_user.get(uid, 0),
            total_spent_kopeks=spending_map.get(uid, 0),
            subscription_start_date=start_date,
            subscription_end_date=end_date,
            last_node_name=last_node_name,
        )

    return enrichment


@router.get('/enrichment', response_model=TrafficEnrichmentResponse)
async def get_traffic_enrichment(
    admin: User = Depends(get_current_admin_user),
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
        enrichment = await _build_enrichment(db, user_map)

        _enrichment_cache[cache_key] = (now, enrichment)

        # Evict expired
        expired = [k for k, (ts, _) in _enrichment_cache.items() if (now - ts) >= _ENRICHMENT_CACHE_TTL]
        for k in expired:
            del _enrichment_cache[k]

        return TrafficEnrichmentResponse(data=enrichment)


@router.post('/export-csv', response_model=ExportCsvResponse)
async def export_traffic_csv(
    request: ExportCsvRequest,
    admin: User = Depends(get_current_admin_user),
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
    else:
        _validate_period(request.period)
        start_str, end_str = _compute_date_range(request.period)
        period_label = f'{request.period}d'

    user_map = await _load_user_map(db)
    user_traffic, nodes_info = await _aggregate_traffic(start_str, end_str, list(user_map.keys()))
    enrichment = await _build_enrichment(db, user_map)

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

    # Compute period days for risk calculation
    if request.start_date and request.end_date:
        period_days = max((end_dt - start_dt).days, 1)
    else:
        period_days = request.period

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
        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        async with bot:
            await bot.send_document(
                chat_id=admin.telegram_id,
                document=BufferedInputFile(csv_bytes, filename=filename),
                caption=f'Traffic usage report ({period_label})\nUsers: {len(rows)}',
            )
    except Exception:
        logger.error('Failed to send CSV to admin %s', admin.telegram_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to send CSV report. Please try again later.',
        )

    return ExportCsvResponse(success=True, message=f'CSV sent ({len(rows)} users)')
