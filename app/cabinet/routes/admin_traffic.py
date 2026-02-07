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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, User
from app.services.remnawave_service import RemnaWaveService

from ..dependencies import get_cabinet_db, get_current_admin_user
from ..schemas.traffic import (
    ExportCsvRequest,
    ExportCsvResponse,
    TrafficNodeInfo,
    TrafficUsageResponse,
    UserTrafficItem,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/admin/traffic', tags=['Admin Traffic'])

_ALLOWED_PERIODS = frozenset({1, 3, 7, 14, 30})
_CONCURRENCY_LIMIT = 5  # Max parallel API calls to avoid rate limiting

# In-memory cache: {period_days: (timestamp, aggregated_data, nodes_info)}
_traffic_cache: dict[int, tuple[float, dict[str, dict[str, int]], list[TrafficNodeInfo]]] = {}
_CACHE_TTL = 300  # 5 minutes
_cache_lock = asyncio.Lock()

# Valid sort fields for the GET endpoint
_SORT_FIELDS = frozenset({'total_bytes', 'full_name', 'tariff_name', 'device_limit', 'traffic_limit_gb'})


def _validate_period(period: int) -> None:
    if period not in _ALLOWED_PERIODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Period must be one of: {sorted(_ALLOWED_PERIODS)}',
        )


async def _aggregate_traffic(
    period_days: int, user_uuids: list[str]
) -> tuple[dict[str, dict[str, int]], list[TrafficNodeInfo]]:
    """Aggregate per-user traffic across all nodes for a given period.

    Uses per-node endpoint get_bandwidth_stats_node_users() to fetch traffic
    for all users at once per node — O(nodes) API calls instead of O(users).

    Returns (user_traffic, nodes_info) where:
      user_traffic = {remnawave_uuid: {node_uuid: total_bytes, ...}}
      nodes_info = [TrafficNodeInfo, ...]
    """
    # Quick check without lock
    now = time.time()
    cached = _traffic_cache.get(period_days)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1], cached[2]

    # Acquire lock for the slow path
    async with _cache_lock:
        # Re-check after acquiring lock
        now = time.time()
        cached = _traffic_cache.get(period_days)
        if cached and (now - cached[0]) < _CACHE_TTL:
            return cached[1], cached[2]

        service = RemnaWaveService()
        if not service.is_configured:
            return {}, []

        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=period_days)
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')

        user_uuids_set = set(user_uuids)

        async with service.get_api_client() as api:
            nodes = await api.get_all_nodes()

            # Fetch per-node user stats — O(nodes) calls instead of O(users)
            semaphore = asyncio.Semaphore(_CONCURRENCY_LIMIT)

            async def fetch_node_users(node):
                async with semaphore:
                    try:
                        stats = await api.get_bandwidth_stats_node_users(
                            node.uuid,
                            start_str,
                            end_str,
                            top_users_limit=9999,
                        )
                        return node.uuid, stats
                    except Exception:
                        logger.warning('Failed to get traffic for node %s', node.name, exc_info=True)
                        return node.uuid, None

            results = await asyncio.gather(*(fetch_node_users(n) for n in nodes))

        nodes_info: list[TrafficNodeInfo] = [
            TrafficNodeInfo(node_uuid=node.uuid, node_name=node.name, country_code=node.country_code) for node in nodes
        ]
        nodes_info.sort(key=lambda n: n.node_name)

        user_traffic: dict[str, dict[str, int]] = {}
        for node_uuid, stats in results:
            if not isinstance(stats, dict):
                continue
            # Non-legacy response: {series: [{userUuid, nodeUuid, total, ...}], topUsers: [...]}
            for entry in stats.get('series', []):
                uid = entry.get('userUuid', '')
                total = int(entry.get('total', 0))
                if uid and total > 0 and uid in user_uuids_set:
                    user_traffic.setdefault(uid, {})[node_uuid] = user_traffic.get(uid, {}).get(node_uuid, 0) + total

        _traffic_cache[period_days] = (now, user_traffic, nodes_info)
        return user_traffic, nodes_info


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
) -> list[UserTrafficItem]:
    """Merge traffic data with user data, apply search/tariff filters, return sorted list."""
    items: list[UserTrafficItem] = []
    search_lower = search.lower().strip()

    all_uuids = set(user_traffic.keys()) | set(user_map.keys())
    for uuid in all_uuids:
        user = user_map.get(uuid)
        if not user:
            continue

        traffic = user_traffic.get(uuid, {})
        total_bytes = sum(traffic.values())

        full_name = user.full_name
        username = user.username

        if search_lower:
            if search_lower not in (full_name or '').lower() and search_lower not in (username or '').lower():
                continue

        sub = user.subscription
        tariff_name = None
        subscription_status = None
        traffic_limit_gb = 0.0
        device_limit = 1

        if sub:
            subscription_status = sub.actual_status if hasattr(sub, 'actual_status') else sub.status
            traffic_limit_gb = float(sub.traffic_limit_gb or 0)
            device_limit = sub.device_limit or 1
            if sub.tariff:
                tariff_name = sub.tariff.name

        if tariff_filter is not None:
            if (tariff_name or '') not in tariff_filter:
                continue

        items.append(
            UserTrafficItem(
                user_id=user.id,
                telegram_id=user.telegram_id,
                username=username,
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
):
    """Get paginated per-user traffic usage by node."""
    _validate_period(period)

    user_map = await _load_user_map(db)
    user_traffic, nodes_info = await _aggregate_traffic(period, list(user_map.keys()))

    # Collect all available tariff names (before filtering)
    available_tariffs = sorted(
        {
            u.subscription.tariff.name
            for u in user_map.values()
            if u.subscription and u.subscription.tariff and u.subscription.tariff.name
        }
    )

    # Parse tariff filter
    tariff_filter: set[str] | None = None
    if tariffs.strip():
        tariff_filter = {t.strip() for t in tariffs.split(',') if t.strip()}

    # Validate sort_by: allow known fields + 'node_<uuid>' for dynamic node columns
    node_uuids = {n.node_uuid for n in nodes_info}
    is_node_sort = sort_by.startswith('node_') and sort_by[5:] in node_uuids
    if sort_by not in _SORT_FIELDS and not is_node_sort:
        sort_by = 'total_bytes'

    items = _build_traffic_items(user_traffic, user_map, nodes_info, search, sort_by, sort_desc, tariff_filter)

    total = len(items)
    paginated = items[offset : offset + limit]

    return TrafficUsageResponse(
        items=paginated,
        nodes=nodes_info,
        total=total,
        offset=offset,
        limit=limit,
        period_days=period,
        available_tariffs=available_tariffs,
    )


@router.post('/export-csv', response_model=ExportCsvResponse)
async def export_traffic_csv(
    request: ExportCsvRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Generate CSV with traffic usage and send to admin's Telegram DM."""
    _validate_period(request.period)

    if not admin.telegram_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Admin has no Telegram ID configured',
        )

    user_map = await _load_user_map(db)
    user_traffic, nodes_info = await _aggregate_traffic(request.period, list(user_map.keys()))
    items = _build_traffic_items(user_traffic, user_map, nodes_info)

    # Build CSV rows
    rows: list[dict] = []
    for item in items:
        row: dict = {
            'User ID': item.user_id,
            'Telegram ID': item.telegram_id or '',
            'Username': item.username or '',
            'Full Name': item.full_name,
            'Tariff': item.tariff_name or '',
            'Status': item.subscription_status or '',
            'Traffic Limit (GB)': item.traffic_limit_gb,
            'Devices': item.device_limit,
        }
        for node in nodes_info:
            row[f'{node.node_name} (bytes)'] = item.node_traffic.get(node.node_uuid, 0)
        row['Total (bytes)'] = item.total_bytes
        row['Total (GB)'] = round(item.total_bytes / (1024**3), 2) if item.total_bytes else 0
        rows.append(row)

    # Generate CSV
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    csv_bytes = output.getvalue().encode('utf-8-sig')

    timestamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
    filename = f'traffic_usage_{request.period}d_{timestamp}.csv'

    try:
        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        async with bot:
            await bot.send_document(
                chat_id=admin.telegram_id,
                document=BufferedInputFile(csv_bytes, filename=filename),
                caption=f'Traffic usage report ({request.period}d)\nUsers: {len(rows)}',
            )
    except Exception:
        logger.error('Failed to send CSV to admin %s', admin.telegram_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to send CSV report. Please try again later.',
        )

    return ExportCsvResponse(success=True, message=f'CSV sent ({len(rows)} users)')
