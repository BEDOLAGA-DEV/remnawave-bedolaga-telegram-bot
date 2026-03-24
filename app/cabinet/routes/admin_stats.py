"""Admin routes for statistics dashboard in cabinet."""

import sys
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.campaign import get_campaign_statistics, get_campaigns_count, get_campaigns_list
from app.database.crud.server_squad import get_server_statistics
from app.database.crud.subscription import get_subscriptions_statistics
from app.database.crud.transaction import REAL_PAYMENT_METHODS, get_revenue_by_period, get_transactions_statistics
from app.database.models import (
    ReferralEarning,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.services.remnawave_service import RemnaWaveService
from app.services.version_service import version_service

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

_start_time = time.time()

router = APIRouter(prefix='/admin/stats', tags=['Cabinet Admin Stats'])


# ============ Schemas ============


class NodeStatus(BaseModel):
    """Node status info."""

    uuid: str
    name: str
    address: str
    is_connected: bool
    is_disabled: bool
    users_online: int
    traffic_used_bytes: int | None = None
    uptime: str | None = None
    xray_version: str | None = None
    node_version: str | None = None
    last_status_message: str | None = None
    xray_uptime: str | None = None
    is_xray_running: bool | None = None
    cpu_count: int | None = None
    cpu_model: str | None = None
    total_ram: str | None = None
    country_code: str | None = None


class NodesOverview(BaseModel):
    """Overview of all nodes."""

    total: int
    online: int
    offline: int
    disabled: int
    total_users_online: int
    nodes: list[NodeStatus]


class RevenueData(BaseModel):
    """Revenue data point."""

    date: str
    amount_kopeks: int
    amount_rubles: float


class SubscriptionStats(BaseModel):
    """Subscription statistics."""

    total: int
    active: int
    trial: int
    paid: int
    expired: int
    purchased_today: int
    purchased_week: int
    purchased_month: int
    trial_to_paid_conversion: float


class FinancialStats(BaseModel):
    """Financial statistics."""

    income_today_kopeks: int
    income_today_rubles: float
    income_month_kopeks: int
    income_month_rubles: float
    income_total_kopeks: int
    income_total_rubles: float
    subscription_income_kopeks: int
    subscription_income_rubles: float


class ServerStats(BaseModel):
    """Server statistics."""

    total_servers: int
    available_servers: int
    servers_with_connections: int
    total_revenue_kopeks: int
    total_revenue_rubles: float


class TariffStatItem(BaseModel):
    """Statistics for a single tariff."""

    tariff_id: int
    tariff_name: str
    active_subscriptions: int
    trial_subscriptions: int
    purchased_today: int
    purchased_week: int
    purchased_month: int


class TariffStats(BaseModel):
    """Tariff statistics."""

    tariffs: list[TariffStatItem]
    total_tariff_subscriptions: int


class DashboardStats(BaseModel):
    """Complete dashboard statistics."""

    nodes: NodesOverview
    subscriptions: SubscriptionStats
    financial: FinancialStats
    servers: ServerStats
    revenue_chart: list[RevenueData]
    tariff_stats: TariffStats | None = None
    subscription_chart: list | None = None  # paid sales (transactions)


class SystemInfoResponse(BaseModel):
    """System information for admin dashboard."""

    bot_version: str
    python_version: str
    uptime_seconds: int
    users_total: int
    subscriptions_active: int


# ============ Extended Stats Schemas ============


class TopReferrerItem(BaseModel):
    """Single referrer in top list."""

    user_id: int
    telegram_id: int | None = None  # Can be None for email-only users
    email: str | None = None
    username: str | None = None
    display_name: str
    invited_count: int
    invited_today: int = 0
    invited_week: int = 0
    invited_month: int = 0
    earnings_today_kopeks: int = 0
    earnings_week_kopeks: int = 0
    earnings_month_kopeks: int = 0
    earnings_total_kopeks: int = 0


class TopReferrersResponse(BaseModel):
    """Top referrers response."""

    by_earnings: list[TopReferrerItem]
    by_invited: list[TopReferrerItem]
    total_referrers: int
    total_referrals: int
    total_earnings_kopeks: int


class TopCampaignItem(BaseModel):
    """Single campaign in top list."""

    id: int
    name: str
    start_parameter: str
    bonus_type: str
    is_active: bool
    registrations: int
    conversions: int
    conversion_rate: float
    total_revenue_kopeks: int
    avg_revenue_per_user_kopeks: int
    created_at: str | None = None


class TopCampaignsResponse(BaseModel):
    """Top campaigns response."""

    campaigns: list[TopCampaignItem]
    total_campaigns: int
    total_registrations: int
    total_revenue_kopeks: int


class RecentPaymentItem(BaseModel):
    """Single recent payment."""

    id: int
    user_id: int
    telegram_id: int | None = None  # Can be None for email-only users
    email: str | None = None
    username: str | None = None
    display_name: str
    amount_kopeks: int
    amount_rubles: float
    type: str
    type_display: str
    payment_method: str | None = None
    description: str | None = None
    created_at: str
    is_completed: bool


class RecentPaymentsResponse(BaseModel):
    """Recent payments response."""

    payments: list[RecentPaymentItem]
    total_count: int
    total_today_kopeks: int
    total_week_kopeks: int


# ============ Routes ============


def _resolve_tz(tz: str | None) -> ZoneInfo:
    """Resolve timezone string to ZoneInfo, fallback to UTC."""
    if tz:
        try:
            return ZoneInfo(tz)
        except (KeyError, ValueError):
            pass
    return ZoneInfo('UTC')


def _today_start_utc(tz: str | None) -> datetime:
    """Get start of today in user's timezone, converted to UTC."""
    user_tz = _resolve_tz(tz)
    now_local = datetime.now(user_tz)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(UTC)


@router.get('/dashboard', response_model=DashboardStats)
async def get_dashboard_stats(
    tz: str | None = Query(None, description='Browser timezone (e.g. Europe/Moscow)'),
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get complete dashboard statistics for admin panel."""
    try:
        # Get nodes status from RemnaWave
        nodes_data = await _get_nodes_overview()

        # Get subscription statistics
        sub_stats = await get_subscriptions_statistics(db, tz=tz)

        # Get financial statistics
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        trans_stats = await get_transactions_statistics(db, month_start, now)
        all_time_stats = await get_transactions_statistics(
            db, start_date=datetime(2020, 1, 1, tzinfo=UTC), end_date=now
        )

        # Get revenue chart data (last 30 days)
        revenue_data = await get_revenue_by_period(db, days=30, tz=tz)

        # Get server statistics
        server_stats = await get_server_statistics(db)

        # Get tariff statistics
        tariff_stats = await _get_tariff_stats(db, tz=tz)

        # Derive income_today from revenue_chart to ensure consistency with chart
        user_tz = _resolve_tz(tz)
        today_str = datetime.now(user_tz).date().isoformat()
        income_today_from_chart = sum(
            item.get('amount_kopeks', 0) for item in revenue_data if str(item.get('date', '')) == today_str
        )
        # Use chart-derived value if available, otherwise fall back to trans_stats
        income_today_kopeks = income_today_from_chart or trans_stats.get('today', {}).get('income_kopeks', 0)

        # Get paid subscription sales chart (last 30 days)
        # Counts new paid subscriptions (is_trial=False), NOT transactions
        try:
            from sqlalchemy import text as _text

            _tz_safe = (tz or 'UTC').replace("'", '')
            _chart_rows = (
                await db.execute(
                    _text(
                        f"SELECT DATE(created_at AT TIME ZONE '{_tz_safe}') as d, COUNT(*) as c "
                        f'FROM subscriptions '
                        f'WHERE is_trial = false '
                        f"AND created_at >= NOW() - INTERVAL '30 days' "
                        f"GROUP BY DATE(created_at AT TIME ZONE '{_tz_safe}') ORDER BY d"
                    )
                )
            ).all()
            subscription_chart = [{'date': str(r[0]), 'count': r[1]} for r in _chart_rows]
        except Exception as _e:
            logger.error('subscription_chart query failed', error=str(_e), error_type=type(_e).__name__)
            subscription_chart = []

        # Build response
        return DashboardStats(
            nodes=nodes_data,
            subscriptions=SubscriptionStats(
                total=sub_stats.get('total_subscriptions', 0),
                active=sub_stats.get('active_subscriptions', 0),
                trial=sub_stats.get('trial_subscriptions', 0),
                paid=sub_stats.get('paid_subscriptions', 0),
                expired=sub_stats.get('total_subscriptions', 0) - sub_stats.get('active_subscriptions', 0),
                purchased_today=sub_stats.get('purchased_today', 0),
                purchased_week=sub_stats.get('purchased_week', 0),
                purchased_month=sub_stats.get('purchased_month', 0),
                trial_to_paid_conversion=sub_stats.get('trial_to_paid_conversion', 0.0),
            ),
            financial=FinancialStats(
                income_today_kopeks=income_today_kopeks,
                income_today_rubles=income_today_kopeks / 100,
                income_month_kopeks=trans_stats.get('totals', {}).get('income_kopeks', 0),
                income_month_rubles=trans_stats.get('totals', {}).get('income_kopeks', 0) / 100,
                income_total_kopeks=all_time_stats.get('totals', {}).get('income_kopeks', 0),
                income_total_rubles=all_time_stats.get('totals', {}).get('income_kopeks', 0) / 100,
                subscription_income_kopeks=abs(all_time_stats.get('totals', {}).get('subscription_income_kopeks', 0)),
                subscription_income_rubles=abs(all_time_stats.get('totals', {}).get('subscription_income_kopeks', 0))
                / 100,
            ),
            servers=ServerStats(
                total_servers=server_stats.get('total_servers', 0),
                available_servers=server_stats.get('available_servers', 0),
                servers_with_connections=server_stats.get('servers_with_connections', 0),
                total_revenue_kopeks=server_stats.get('total_revenue_kopeks', 0),
                total_revenue_rubles=server_stats.get('total_revenue_rubles', 0.0),
            ),
            revenue_chart=[
                RevenueData(
                    date=item.get('date', '').isoformat()
                    if hasattr(item.get('date', ''), 'isoformat')
                    else str(item.get('date', '')),
                    amount_kopeks=item.get('amount_kopeks', 0),
                    amount_rubles=item.get('amount_kopeks', 0) / 100,
                )
                for item in revenue_data
            ],
            tariff_stats=tariff_stats,
            subscription_chart=subscription_chart,
        )

    except Exception as e:
        logger.error('Failed to get dashboard stats', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load dashboard statistics',
        )


@router.get('/system-info', response_model=SystemInfoResponse)
async def get_system_info(
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get system information for admin dashboard."""
    try:
        users_total_result = await db.execute(select(func.count()).select_from(User))
        users_total = users_total_result.scalar() or 0

        subs_active_result = await db.execute(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
        )
        subscriptions_active = subs_active_result.scalar() or 0

        return SystemInfoResponse(
            bot_version=version_service.current_version,
            python_version=sys.version.split()[0],
            uptime_seconds=int(time.time() - _start_time),
            users_total=users_total,
            subscriptions_active=subscriptions_active,
        )
    except Exception as e:
        logger.error('Failed to get system info', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load system information',
        )


@router.get('/nodes', response_model=NodesOverview)
async def get_nodes_status(
    admin: User = Depends(require_permission('stats:read')),
):
    """Get status of all nodes."""
    try:
        return await _get_nodes_overview()
    except Exception as e:
        logger.error('Failed to get nodes status', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load nodes status',
        )


@router.post('/nodes/{node_uuid}/restart')
async def restart_node(
    node_uuid: str,
    admin: User = Depends(require_permission('remnawave:manage')),
):
    """Restart a node."""
    try:
        service = RemnaWaveService()
        success = await service.manage_node(node_uuid, 'restart')

        if success:
            logger.info('Admin restarted node', admin_id=admin.id, node_uuid=node_uuid)
            return {'success': True, 'message': 'Node restart initiated'}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to restart node',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to restart node', node_uuid=node_uuid, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to restart node',
        )


@router.post('/nodes/{node_uuid}/toggle')
async def toggle_node(
    node_uuid: str,
    admin: User = Depends(require_permission('remnawave:manage')),
):
    """Enable or disable a node."""
    try:
        service = RemnaWaveService()
        nodes = await service.get_all_nodes()

        node = next((n for n in nodes if n.get('uuid') == node_uuid), None)
        if not node:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Node not found',
            )

        is_disabled = node.get('is_disabled', False)
        action = 'enable' if is_disabled else 'disable'
        success = await service.manage_node(node_uuid, action)

        if success:
            logger.info('Admin d node', admin_id=admin.id, action=action, node_uuid=node_uuid)
            return {'success': True, 'message': f'Node {action}d', 'is_disabled': not is_disabled}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Failed to {action} node',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to toggle node', node_uuid=node_uuid, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle node',
        )


async def _get_nodes_overview() -> NodesOverview:
    """Get overview of all nodes."""
    try:
        service = RemnaWaveService()
        nodes = await service.get_all_nodes()

        total = len(nodes)
        online = sum(1 for n in nodes if n.get('is_connected') and not n.get('is_disabled'))
        disabled = sum(1 for n in nodes if n.get('is_disabled'))
        offline = total - online - disabled
        total_users_online = sum(n.get('users_online', 0) or 0 for n in nodes)

        node_statuses = [
            NodeStatus(
                uuid=n.get('uuid', ''),
                name=n.get('name', 'Unknown'),
                address=n.get('address', ''),
                is_connected=n.get('is_connected', False),
                is_disabled=n.get('is_disabled', False),
                users_online=n.get('users_online', 0) or 0,
                traffic_used_bytes=n.get('traffic_used_bytes'),
                uptime=n.get('uptime'),
                xray_version=n.get('xray_version'),
                node_version=n.get('node_version'),
                last_status_message=n.get('last_status_message'),
                xray_uptime=n.get('xray_uptime'),
                is_xray_running=n.get('is_xray_running'),
                cpu_count=n.get('cpu_count'),
                cpu_model=n.get('cpu_model'),
                total_ram=n.get('total_ram'),
                country_code=n.get('country_code'),
            )
            for n in nodes
        ]

        return NodesOverview(
            total=total,
            online=online,
            offline=offline,
            disabled=disabled,
            total_users_online=total_users_online,
            nodes=node_statuses,
        )
    except Exception as e:
        logger.warning('Failed to get nodes from RemnaWave', error=e)
        # Return empty data if RemnaWave is unavailable
        return NodesOverview(
            total=0,
            online=0,
            offline=0,
            disabled=0,
            total_users_online=0,
            nodes=[],
        )


async def _get_tariff_stats(db: AsyncSession, tz: str | None = None) -> TariffStats | None:
    """Get statistics for all tariffs."""
    try:
        # Получаем ВСЕ тарифы (включая неактивные) для статистики
        tariffs_result = await db.execute(select(Tariff).order_by(Tariff.display_order))
        tariffs = tariffs_result.scalars().all()

        if not tariffs:
            logger.info('📊 Нет тарифов в системе, пропускаем статистику')
            return None

        now = datetime.now(UTC)
        today_start = _today_start_utc(tz)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        tariff_items = []
        total_tariff_subscriptions = 0

        for tariff in tariffs:
            # Активные подписки на этом тарифе
            active_result = await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.tariff_id == tariff.id, Subscription.status == SubscriptionStatus.ACTIVE.value
                )
            )
            active_count = active_result.scalar() or 0

            # Триальные подписки на этом тарифе
            trial_result = await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.tariff_id == tariff.id,
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.is_trial == True,
                )
            )
            trial_count = trial_result.scalar() or 0

            # Куплено сегодня (не триальные)
            today_result = await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.tariff_id == tariff.id,
                    Subscription.created_at >= today_start,
                    Subscription.is_trial == False,
                )
            )
            purchased_today = today_result.scalar() or 0

            # Куплено за неделю
            week_result = await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.tariff_id == tariff.id,
                    Subscription.created_at >= week_ago,
                    Subscription.is_trial == False,
                )
            )
            purchased_week = week_result.scalar() or 0

            # Куплено за месяц
            month_result = await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.tariff_id == tariff.id,
                    Subscription.created_at >= month_ago,
                    Subscription.is_trial == False,
                )
            )
            purchased_month = month_result.scalar() or 0

            logger.info(
                '📊 Тариф активных=, триал', tariff_name=tariff.name, active_count=active_count, trial_count=trial_count
            )

            tariff_items.append(
                TariffStatItem(
                    tariff_id=tariff.id,
                    tariff_name=tariff.name,
                    active_subscriptions=active_count,
                    trial_subscriptions=trial_count,
                    purchased_today=purchased_today,
                    purchased_week=purchased_week,
                    purchased_month=purchased_month,
                )
            )

            total_tariff_subscriptions += active_count

        logger.info('📊 Всего подписок по тарифам', total_tariff_subscriptions=total_tariff_subscriptions)

        return TariffStats(
            tariffs=tariff_items,
            total_tariff_subscriptions=total_tariff_subscriptions,
        )

    except Exception as e:
        logger.error('Failed to get tariff stats', error=e, exc_info=True)
        return None


# ============ Extended Stats Routes ============


@router.get('/referrals/top', response_model=TopReferrersResponse)
async def get_top_referrers(
    limit: int = 20,
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get top referrers with earnings breakdown by period."""
    try:
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        # Get all referrers with their stats
        referrers_query = await db.execute(
            select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('total_invited'))
            .where(User.referred_by_id.isnot(None))
            .group_by(User.referred_by_id)
        )
        referrers_data = {row.referrer_id: {'total_invited': row.total_invited} for row in referrers_query}

        # Get invited counts by period for each referrer
        # Today
        today_invited_query = await db.execute(
            select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('count'))
            .where(and_(User.referred_by_id.isnot(None), User.created_at >= today_start))
            .group_by(User.referred_by_id)
        )
        for row in today_invited_query:
            if row.referrer_id in referrers_data:
                referrers_data[row.referrer_id]['invited_today'] = row.count

        # Week
        week_invited_query = await db.execute(
            select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('count'))
            .where(and_(User.referred_by_id.isnot(None), User.created_at >= week_ago))
            .group_by(User.referred_by_id)
        )
        for row in week_invited_query:
            if row.referrer_id in referrers_data:
                referrers_data[row.referrer_id]['invited_week'] = row.count

        # Month
        month_invited_query = await db.execute(
            select(User.referred_by_id.label('referrer_id'), func.count(User.id).label('count'))
            .where(and_(User.referred_by_id.isnot(None), User.created_at >= month_ago))
            .group_by(User.referred_by_id)
        )
        for row in month_invited_query:
            if row.referrer_id in referrers_data:
                referrers_data[row.referrer_id]['invited_month'] = row.count

        # Get earnings from ReferralEarning table
        # Total earnings
        total_earnings_query = await db.execute(
            select(
                ReferralEarning.user_id.label('referrer_id'), func.sum(ReferralEarning.amount_kopeks).label('total')
            ).group_by(ReferralEarning.user_id)
        )
        for row in total_earnings_query:
            if row.referrer_id in referrers_data:
                referrers_data[row.referrer_id]['earnings_total'] = row.total or 0

        # Today earnings
        today_earnings_query = await db.execute(
            select(ReferralEarning.user_id.label('referrer_id'), func.sum(ReferralEarning.amount_kopeks).label('total'))
            .where(ReferralEarning.created_at >= today_start)
            .group_by(ReferralEarning.user_id)
        )
        for row in today_earnings_query:
            if row.referrer_id in referrers_data:
                referrers_data[row.referrer_id]['earnings_today'] = row.total or 0

        # Week earnings
        week_earnings_query = await db.execute(
            select(ReferralEarning.user_id.label('referrer_id'), func.sum(ReferralEarning.amount_kopeks).label('total'))
            .where(ReferralEarning.created_at >= week_ago)
            .group_by(ReferralEarning.user_id)
        )
        for row in week_earnings_query:
            if row.referrer_id in referrers_data:
                referrers_data[row.referrer_id]['earnings_week'] = row.total or 0

        # Month earnings
        month_earnings_query = await db.execute(
            select(ReferralEarning.user_id.label('referrer_id'), func.sum(ReferralEarning.amount_kopeks).label('total'))
            .where(ReferralEarning.created_at >= month_ago)
            .group_by(ReferralEarning.user_id)
        )
        for row in month_earnings_query:
            if row.referrer_id in referrers_data:
                referrers_data[row.referrer_id]['earnings_month'] = row.total or 0

        # Get user info for all referrers
        referrer_ids = list(referrers_data.keys())
        if referrer_ids:
            users_query = await db.execute(
                select(User.id, User.telegram_id, User.username, User.first_name, User.last_name, User.email).where(
                    User.id.in_(referrer_ids)
                )
            )
            users_info = {u.id: u for u in users_query}
        else:
            users_info = {}

        # Build referrer items
        referrer_items = []
        for referrer_id, data in referrers_data.items():
            user = users_info.get(referrer_id)
            if not user:
                continue

            display_name = ''
            if user.first_name:
                display_name = user.first_name
                if user.last_name:
                    display_name += f' {user.last_name}'
            elif user.username:
                display_name = f'@{user.username}'
            elif user.telegram_id:
                display_name = f'ID{user.telegram_id}'
            elif user.email:
                display_name = user.email.split('@')[0]
            else:
                display_name = f'User#{user.id}'

            referrer_items.append(
                TopReferrerItem(
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    email=user.email,
                    username=user.username,
                    display_name=display_name,
                    invited_count=data.get('total_invited', 0),
                    invited_today=data.get('invited_today', 0),
                    invited_week=data.get('invited_week', 0),
                    invited_month=data.get('invited_month', 0),
                    earnings_today_kopeks=data.get('earnings_today', 0),
                    earnings_week_kopeks=data.get('earnings_week', 0),
                    earnings_month_kopeks=data.get('earnings_month', 0),
                    earnings_total_kopeks=data.get('earnings_total', 0),
                )
            )

        # Sort by earnings and by invited
        by_earnings = sorted(referrer_items, key=lambda x: x.earnings_total_kopeks, reverse=True)[:limit]
        by_invited = sorted(referrer_items, key=lambda x: x.invited_count, reverse=True)[:limit]

        # Calculate totals
        total_referrers = len(referrer_items)
        total_referrals = sum(r.invited_count for r in referrer_items)
        total_earnings = sum(r.earnings_total_kopeks for r in referrer_items)

        return TopReferrersResponse(
            by_earnings=by_earnings,
            by_invited=by_invited,
            total_referrers=total_referrers,
            total_referrals=total_referrals,
            total_earnings_kopeks=total_earnings,
        )

    except Exception as e:
        logger.error('Failed to get top referrers', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load referrers statistics',
        )


@router.get('/campaigns/top', response_model=TopCampaignsResponse)
async def get_top_campaigns(
    limit: int = 20,
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get top advertising campaigns with statistics."""
    try:
        # Get all campaigns
        campaigns = await get_campaigns_list(db, offset=0, limit=100, include_inactive=True)

        campaign_items = []
        total_registrations = 0
        total_revenue = 0

        for campaign in campaigns:
            stats = await get_campaign_statistics(db, campaign.id)

            campaign_items.append(
                TopCampaignItem(
                    id=campaign.id,
                    name=campaign.name,
                    start_parameter=campaign.start_parameter,
                    bonus_type=campaign.bonus_type,
                    is_active=campaign.is_active,
                    registrations=stats.get('registrations', 0),
                    conversions=stats.get('conversion_count', 0),
                    conversion_rate=stats.get('conversion_rate', 0.0),
                    total_revenue_kopeks=stats.get('total_revenue_kopeks', 0),
                    avg_revenue_per_user_kopeks=stats.get('avg_revenue_per_user_kopeks', 0),
                    created_at=campaign.created_at.isoformat() if campaign.created_at else None,
                )
            )

            total_registrations += stats.get('registrations', 0)
            total_revenue += stats.get('total_revenue_kopeks', 0)

        # Sort by revenue
        campaign_items.sort(key=lambda x: x.total_revenue_kopeks, reverse=True)

        total_campaigns = await get_campaigns_count(db)

        return TopCampaignsResponse(
            campaigns=campaign_items[:limit],
            total_campaigns=total_campaigns,
            total_registrations=total_registrations,
            total_revenue_kopeks=total_revenue,
        )

    except Exception as e:
        logger.error('Failed to get top campaigns', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load campaigns statistics',
        )


@router.get('/payments/recent', response_model=RecentPaymentsResponse)
async def get_recent_payments(
    limit: int = 50,
    admin: User = Depends(require_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get recent payments with user info."""
    try:
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)

        # Get recent transactions (deposits and subscription payments)
        transactions_query = await db.execute(
            select(Transaction)
            .where(
                Transaction.type.in_(
                    [
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    ]
                )
            )
            .order_by(Transaction.created_at.desc())
            .limit(limit)
        )
        transactions = transactions_query.scalars().all()

        # Get user info for all transactions
        user_ids = list({t.user_id for t in transactions})
        if user_ids:
            users_query = await db.execute(
                select(User.id, User.telegram_id, User.username, User.first_name, User.last_name, User.email).where(
                    User.id.in_(user_ids)
                )
            )
            users_info = {u.id: u for u in users_query}
        else:
            users_info = {}

        # Type display names
        type_display = {
            TransactionType.DEPOSIT.value: 'Пополнение',
            TransactionType.SUBSCRIPTION_PAYMENT.value: 'Оплата подписки',
            TransactionType.WITHDRAWAL.value: 'Вывод',
            TransactionType.REFUND.value: 'Возврат',
            TransactionType.REFERRAL_REWARD.value: 'Реферальный бонус',
            TransactionType.POLL_REWARD.value: 'Награда за опрос',
        }

        payment_items = []
        for trans in transactions:
            user = users_info.get(trans.user_id)
            if not user:
                continue

            display_name = ''
            if user.first_name:
                display_name = user.first_name
                if user.last_name:
                    display_name += f' {user.last_name}'
            elif user.username:
                display_name = f'@{user.username}'
            elif user.telegram_id:
                display_name = f'ID{user.telegram_id}'
            elif user.email:
                display_name = user.email.split('@')[0]
            else:
                display_name = f'User#{user.id}'

            payment_items.append(
                RecentPaymentItem(
                    id=trans.id,
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    email=user.email,
                    username=user.username,
                    display_name=display_name,
                    amount_kopeks=abs(trans.amount_kopeks),
                    amount_rubles=abs(trans.amount_kopeks) / 100,
                    type=trans.type,
                    type_display=type_display.get(trans.type, trans.type),
                    payment_method=trans.payment_method,
                    description=trans.description,
                    created_at=trans.created_at.isoformat() if trans.created_at else '',
                    is_completed=trans.is_completed,
                )
            )

        # Calculate totals
        total_count_result = await db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.type.in_(
                    [
                        TransactionType.DEPOSIT.value,
                        TransactionType.SUBSCRIPTION_PAYMENT.value,
                    ]
                )
            )
        )
        total_count = total_count_result.scalar() or 0

        today_total_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                    Transaction.is_completed == True,
                    Transaction.created_at >= today_start,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                )
            )
        )
        total_today = today_total_result.scalar() or 0

        week_total_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                    Transaction.is_completed == True,
                    Transaction.created_at >= week_ago,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                )
            )
        )
        total_week = week_total_result.scalar() or 0

        return RecentPaymentsResponse(
            payments=payment_items,
            total_count=total_count,
            total_today_kopeks=total_today,
            total_week_kopeks=total_week,
        )

    except Exception as e:
        logger.error('Failed to get recent payments', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load recent payments',
        )
