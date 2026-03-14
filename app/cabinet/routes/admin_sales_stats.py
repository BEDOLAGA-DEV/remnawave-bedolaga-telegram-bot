"""Admin routes for sales statistics in cabinet."""

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Integer as SAInteger, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    Subscription,
    SubscriptionConversion,
    SubscriptionStatus,
    Tariff,
    TrafficPurchase,
    Transaction,
    TransactionType,
    User,
)

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/stats/sales', tags=['Cabinet Admin Sales Stats'])


# ============ Helpers ============

MAX_PERIOD_DAYS = 730  # 2 years max


def _parse_period(
    days: int | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[datetime, datetime]:
    """Parse period from preset days or custom date range."""
    now = datetime.now(UTC)
    if start_date and end_date:
        try:
            start = datetime.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid start_date format',
            )
        try:
            end = datetime.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid end_date format',
            )
        # Ensure timezone awareness
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        # Validate range
        if start > end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='start_date must be before end_date',
            )
        if (end - start).days > MAX_PERIOD_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Date range cannot exceed {MAX_PERIOD_DAYS} days',
            )
        return start, end.replace(hour=23, minute=59, second=59)
    if days is not None and days > 0:
        days = min(days, MAX_PERIOD_DAYS)
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    # Default: all time (from epoch)
    return datetime(2020, 1, 1, tzinfo=UTC), now


# ============ Summary Schemas ============


class SalesSummary(BaseModel):
    """Summary stats for the top cards."""

    total_revenue_kopeks: int
    active_subscriptions: int
    active_trials: int
    new_trials: int
    trial_to_paid_conversion: float
    renewals_count: int
    addon_revenue_kopeks: int


# ============ Summary Endpoint ============


@router.get('/summary', response_model=SalesSummary)
async def get_sales_summary(
    days: int | None = Query(default=30, description='Preset period in days (7, 30, 90, 0=all)'),
    start_date: str | None = Query(default=None, description='Custom start date ISO format'),
    end_date: str | None = Query(default=None, description='Custom end date ISO format'),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SalesSummary:
    """Get summary statistics for sales dashboard cards."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        # Total revenue (deposits with real payment methods)
        revenue_result = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                and_(
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed == True,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        total_revenue = revenue_result.scalar() or 0

        # Consolidated subscription counts: active paid, active trial, new trials in period
        sub_counts_result = await db.execute(
            select(
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.status == SubscriptionStatus.ACTIVE.value, Subscription.is_trial.is_(False)
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('active_paid'),
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.status == SubscriptionStatus.ACTIVE.value, Subscription.is_trial.is_(True)
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('active_trial'),
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.is_trial.is_(True),
                                Subscription.created_at >= period_start,
                                Subscription.created_at <= period_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('new_trials'),
            )
        )
        row = sub_counts_result.one()
        active_subs = row.active_paid or 0
        active_trials = row.active_trial or 0
        new_trials = row.new_trials or 0

        # Trial-to-paid conversion in period
        conversions_result = await db.execute(
            select(func.count(SubscriptionConversion.id)).where(
                and_(
                    SubscriptionConversion.converted_at >= period_start,
                    SubscriptionConversion.converted_at <= period_end,
                )
            )
        )
        conversions = conversions_result.scalar() or 0
        # Cap at 100%: conversions from previous periods can exceed current new_trials
        conversion_rate = min(round((conversions / new_trials * 100), 1), 100.0) if new_trials > 0 else 0.0

        # Renewals count
        renewals_subquery = (
            select(Transaction.user_id)
            .where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at < period_start,
                )
            )
            .distinct()
        )
        renewals_result = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                    Transaction.user_id.in_(renewals_subquery),
                )
            )
        )
        renewals_count = renewals_result.scalar() or 0

        # Add-on revenue
        addon_revenue_result = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount_kopeks), 0)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.description.ilike('%трафик%'),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        addon_revenue = abs(addon_revenue_result.scalar() or 0)

        return SalesSummary(
            total_revenue_kopeks=total_revenue,
            active_subscriptions=active_subs,
            active_trials=active_trials,
            new_trials=new_trials,
            trial_to_paid_conversion=conversion_rate,
            renewals_count=renewals_count,
            addon_revenue_kopeks=addon_revenue,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get sales summary', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load sales summary',
        )


# ============ Trials Schemas ============


class ProviderBreakdownItem(BaseModel):
    provider: str
    count: int


class DailyTrialItem(BaseModel):
    date: str
    registrations: int
    trials: int


class TrialsStatsResponse(BaseModel):
    total_trials: int
    total_registrations: int
    conversion_rate: float
    avg_trial_duration_days: float
    by_provider: list[ProviderBreakdownItem]
    daily: list[DailyTrialItem]


# ============ Trials Endpoint ============


@router.get('/trials', response_model=TrialsStatsResponse)
async def get_trials_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> TrialsStatsResponse:
    """Get trial registration statistics with provider breakdown."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        total_result = await db.execute(
            select(func.count(Subscription.id)).where(
                and_(
                    Subscription.is_trial == True,
                    Subscription.created_at >= period_start,
                    Subscription.created_at <= period_end,
                )
            )
        )
        total_trials = total_result.scalar() or 0

        conversions_result = await db.execute(
            select(func.count(SubscriptionConversion.id)).where(
                and_(
                    SubscriptionConversion.converted_at >= period_start,
                    SubscriptionConversion.converted_at <= period_end,
                )
            )
        )
        conversions = conversions_result.scalar() or 0
        # Cap at 100%: conversions from previous periods can exceed current period trials
        conversion_rate = min(round((conversions / total_trials * 100), 1), 100.0) if total_trials > 0 else 0.0

        avg_duration_result = await db.execute(
            select(func.avg(SubscriptionConversion.trial_duration_days)).where(
                and_(
                    SubscriptionConversion.converted_at >= period_start,
                    SubscriptionConversion.converted_at <= period_end,
                    SubscriptionConversion.trial_duration_days.isnot(None),
                )
            )
        )
        avg_duration = float(avg_duration_result.scalar() or 0.0)

        provider_case = case(
            (User.vk_id.isnot(None), 'vk'),
            (User.yandex_id.isnot(None), 'yandex'),
            (User.google_id.isnot(None), 'google'),
            (User.discord_id.isnot(None), 'discord'),
            (User.auth_type == 'email', 'email'),
            else_='telegram',
        )
        provider_query = await db.execute(
            select(
                provider_case.label('provider'),
                func.count(Subscription.id).label('count'),
            )
            .join(User, Subscription.user_id == User.id)
            .where(
                and_(
                    Subscription.is_trial == True,
                    Subscription.created_at >= period_start,
                    Subscription.created_at <= period_end,
                )
            )
            .group_by(provider_case)
        )
        by_provider = [ProviderBreakdownItem(provider=row.provider, count=row.count) for row in provider_query]

        # Total registrations (all user signups in period)
        reg_total_result = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.created_at >= period_start,
                    User.created_at <= period_end,
                )
            )
        )
        total_registrations = reg_total_result.scalar() or 0

        # Daily registrations (user signups per day)
        daily_reg_query = await db.execute(
            select(
                func.date(User.created_at).label('date'),
                func.count(User.id).label('count'),
            )
            .where(
                and_(
                    User.created_at >= period_start,
                    User.created_at <= period_end,
                )
            )
            .group_by(func.date(User.created_at))
            .order_by(func.date(User.created_at))
        )
        reg_by_date: dict[str, int] = {}
        for row in daily_reg_query:
            date_str = row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date)
            reg_by_date[date_str] = row.count

        # Daily trials (trial subscriptions per day)
        daily_trial_query = await db.execute(
            select(
                func.date(Subscription.created_at).label('date'),
                func.count(Subscription.id).label('count'),
            )
            .where(
                and_(
                    Subscription.is_trial == True,
                    Subscription.created_at >= period_start,
                    Subscription.created_at <= period_end,
                )
            )
            .group_by(func.date(Subscription.created_at))
            .order_by(func.date(Subscription.created_at))
        )
        trial_by_date: dict[str, int] = {}
        for row in daily_trial_query:
            date_str = row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date)
            trial_by_date[date_str] = row.count

        # Merge both series by date union
        all_dates = sorted(set(reg_by_date.keys()) | set(trial_by_date.keys()))
        daily = [
            DailyTrialItem(
                date=d,
                registrations=reg_by_date.get(d, 0),
                trials=trial_by_date.get(d, 0),
            )
            for d in all_dates
        ]

        return TrialsStatsResponse(
            total_trials=total_trials,
            total_registrations=total_registrations,
            conversion_rate=conversion_rate,
            avg_trial_duration_days=round(avg_duration, 1),
            by_provider=by_provider,
            daily=daily,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get trials stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load trials statistics',
        )


# ============ Sales Schemas ============


class SalesByTariffItem(BaseModel):
    tariff_id: int
    tariff_name: str
    count: int


class SalesByPeriodItem(BaseModel):
    period_days: int
    count: int


class DailySalesItem(BaseModel):
    date: str
    count: int
    revenue_kopeks: int


class DailyTariffSalesItem(BaseModel):
    date: str
    tariff_name: str
    count: int


class SalesStatsResponse(BaseModel):
    total_sales: int
    total_revenue_kopeks: int
    avg_order_kopeks: int
    top_tariff_name: str
    by_tariff: list[SalesByTariffItem]
    by_period: list[SalesByPeriodItem]
    daily: list[DailySalesItem]
    daily_by_tariff: list[DailyTariffSalesItem]


# ============ Sales Endpoint ============


@router.get('/subscriptions', response_model=SalesStatsResponse)
async def get_sales_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SalesStatsResponse:
    """Get subscription sales statistics."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        base_filter = and_(
            Subscription.is_trial == False,
            Subscription.created_at >= period_start,
            Subscription.created_at <= period_end,
        )

        totals_result = await db.execute(select(func.count(Subscription.id).label('count')).where(base_filter))
        totals = totals_result.one()
        total_sales = totals.count

        revenue_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        total_revenue = revenue_result.scalar() or 0
        avg_order = total_revenue // total_sales if total_sales > 0 else 0

        by_tariff_query = await db.execute(
            select(
                Tariff.id.label('tariff_id'),
                Tariff.name.label('tariff_name'),
                func.count(Subscription.id).label('count'),
            )
            .join(Tariff, Subscription.tariff_id == Tariff.id, isouter=True)
            .where(base_filter)
            .group_by(Tariff.id, Tariff.name)
            .order_by(func.count(Subscription.id).desc())
        )
        by_tariff = []
        top_tariff_name = '-'
        for i, row in enumerate(by_tariff_query):
            name = row.tariff_name or 'Unknown'
            by_tariff.append(
                SalesByTariffItem(
                    tariff_id=row.tariff_id or 0,
                    tariff_name=name,
                    count=row.count,
                )
            )
            if i == 0:
                top_tariff_name = name

        # Use epoch extraction / 86400 for correct total days (EXTRACT(day) only returns the day component)
        period_days_expr = cast(
            func.extract('epoch', Subscription.end_date - Subscription.start_date) / 86400,
            SAInteger,
        )
        by_period_query = await db.execute(
            select(
                period_days_expr.label('period_days'),
                func.count(Subscription.id).label('count'),
            )
            .where(base_filter)
            .group_by(period_days_expr)
            .order_by(period_days_expr)
        )
        by_period = [
            SalesByPeriodItem(period_days=int(row.period_days or 0), count=row.count) for row in by_period_query
        ]

        daily_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
            )
            .where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily = [
            DailySalesItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
                revenue_kopeks=row.revenue,
            )
            for row in daily_query
        ]

        # Daily sales grouped by tariff
        tariff_name_col = func.coalesce(Tariff.name, 'Unknown')
        daily_by_tariff_query = await db.execute(
            select(
                func.date(Subscription.created_at).label('date'),
                tariff_name_col.label('tariff_name'),
                func.count(Subscription.id).label('count'),
            )
            .join(Tariff, Subscription.tariff_id == Tariff.id, isouter=True)
            .where(base_filter)
            .group_by(func.date(Subscription.created_at), tariff_name_col)
            .order_by(func.date(Subscription.created_at), tariff_name_col)
        )
        daily_by_tariff = [
            DailyTariffSalesItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                tariff_name=row.tariff_name,
                count=row.count,
            )
            for row in daily_by_tariff_query
        ]

        return SalesStatsResponse(
            total_sales=total_sales,
            total_revenue_kopeks=total_revenue,
            avg_order_kopeks=avg_order,
            top_tariff_name=top_tariff_name,
            by_tariff=by_tariff,
            by_period=by_period,
            daily=daily,
            daily_by_tariff=daily_by_tariff,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get sales stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load sales statistics',
        )


# ============ Renewals Schemas ============


class DailyRenewalItem(BaseModel):
    date: str
    count: int


class RenewalPeriodStats(BaseModel):
    count: int
    revenue_kopeks: int


class RenewalChange(BaseModel):
    absolute: int
    percent: float
    trend: str


class RenewalsStatsResponse(BaseModel):
    total_renewals: int
    total_revenue_kopeks: int
    renewal_rate: float
    current_period: RenewalPeriodStats
    previous_period: RenewalPeriodStats
    change: RenewalChange
    daily: list[DailyRenewalItem]


# ============ Renewals Endpoint ============


@router.get('/renewals', response_model=RenewalsStatsResponse)
async def get_renewals_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RenewalsStatsResponse:
    """Get renewal statistics with period comparison."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)
        is_all_time = days is not None and days == 0

        if is_all_time:
            # For "all time": renewals = users with more than 1 subscription payment
            repeat_users_subquery = (
                select(Transaction.user_id)
                .where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                    )
                )
                .group_by(Transaction.user_id)
                .having(func.count(Transaction.id) > 1)
            )
            existing_users_subquery = repeat_users_subquery

            current_result = await db.execute(
                select(
                    func.count(Transaction.id).label('count'),
                    func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
                ).where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        Transaction.user_id.in_(repeat_users_subquery),
                    )
                )
            )
            current = current_result.one()
            current_count = current.count
            current_revenue = current.revenue

            # No meaningful previous period for "all time"
            prev = type('Row', (), {'count': 0, 'revenue': 0})()
        else:
            period_length = period_end - period_start
            prev_start = period_start - period_length
            prev_end = period_start

            existing_users_subquery = (
                select(Transaction.user_id)
                .where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        Transaction.created_at < period_start,
                    )
                )
                .distinct()
            )

            current_result = await db.execute(
                select(
                    func.count(Transaction.id).label('count'),
                    func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
                ).where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        Transaction.created_at >= period_start,
                        Transaction.created_at <= period_end,
                        Transaction.user_id.in_(existing_users_subquery),
                    )
                )
            )
            current = current_result.one()
            current_count = current.count
            current_revenue = current.revenue

            prev_existing_subquery = (
                select(Transaction.user_id)
                .where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        Transaction.created_at < prev_start,
                    )
                )
                .distinct()
            )
            prev_result = await db.execute(
                select(
                    func.count(Transaction.id).label('count'),
                    func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
                ).where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        Transaction.created_at >= prev_start,
                        Transaction.created_at <= prev_end,
                        Transaction.user_id.in_(prev_existing_subquery),
                    )
                )
            )
            prev = prev_result.one()

        if prev.count > 0:
            change_percent = round(((current_count - prev.count) / prev.count) * 100, 1)
        else:
            change_percent = 100.0 if current_count > 0 else 0.0

        if change_percent > 0:
            trend = 'up'
        elif change_percent < 0:
            trend = 'down'
        else:
            trend = 'stable'

        total_sub_payments_result = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        total_sub_payments = total_sub_payments_result.scalar() or 0
        renewal_rate = round((current_count / total_sub_payments * 100), 1) if total_sub_payments > 0 else 0.0

        daily_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
            )
            .where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                    Transaction.user_id.in_(existing_users_subquery),
                )
            )
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily = [
            DailyRenewalItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
            )
            for row in daily_query
        ]

        return RenewalsStatsResponse(
            total_renewals=current_count,
            total_revenue_kopeks=current_revenue,
            renewal_rate=renewal_rate,
            current_period=RenewalPeriodStats(count=current_count, revenue_kopeks=current_revenue),
            previous_period=RenewalPeriodStats(count=prev.count, revenue_kopeks=prev.revenue),
            change=RenewalChange(
                absolute=current_count - prev.count,
                percent=change_percent,
                trend=trend,
            ),
            daily=daily,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get renewals stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load renewals statistics',
        )


# ============ Add-ons Schemas ============


class AddonByPackageItem(BaseModel):
    traffic_gb: int
    count: int


class DailyAddonItem(BaseModel):
    date: str
    count: int
    total_gb: int


class DailyDeviceItem(BaseModel):
    date: str
    count: int


class AddonsStatsResponse(BaseModel):
    total_purchases: int
    total_gb_purchased: int
    addon_revenue_kopeks: int
    device_purchases: int
    device_revenue_kopeks: int
    by_package: list[AddonByPackageItem]
    daily: list[DailyAddonItem]
    daily_devices: list[DailyDeviceItem]


# ============ Add-ons Endpoint ============


@router.get('/addons', response_model=AddonsStatsResponse)
async def get_addons_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AddonsStatsResponse:
    """Get add-on purchase statistics."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        base_filter = and_(
            TrafficPurchase.created_at >= period_start,
            TrafficPurchase.created_at <= period_end,
        )

        totals_result = await db.execute(
            select(
                func.count(TrafficPurchase.id).label('count'),
                func.coalesce(func.sum(TrafficPurchase.traffic_gb), 0).label('total_gb'),
            ).where(base_filter)
        )
        totals = totals_result.one()

        addon_revenue_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.description.ilike('%трафик%'),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        addon_revenue = addon_revenue_result.scalar() or 0

        by_package_query = await db.execute(
            select(
                TrafficPurchase.traffic_gb.label('traffic_gb'),
                func.count(TrafficPurchase.id).label('count'),
            )
            .where(base_filter)
            .group_by(TrafficPurchase.traffic_gb)
            .order_by(TrafficPurchase.traffic_gb)
        )
        by_package = [AddonByPackageItem(traffic_gb=row.traffic_gb, count=row.count) for row in by_package_query]

        daily_query = await db.execute(
            select(
                func.date(TrafficPurchase.created_at).label('date'),
                func.count(TrafficPurchase.id).label('count'),
                func.coalesce(func.sum(TrafficPurchase.traffic_gb), 0).label('total_gb'),
            )
            .where(base_filter)
            .group_by(func.date(TrafficPurchase.created_at))
            .order_by(func.date(TrafficPurchase.created_at))
        )
        daily = [
            DailyAddonItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
                total_gb=row.total_gb,
            )
            for row in daily_query
        ]

        # Device purchases (transactions with 'устройств' in description)
        device_filter = and_(
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed == True,
            Transaction.description.ilike('%устройств%'),
            Transaction.created_at >= period_start,
            Transaction.created_at <= period_end,
        )
        device_result = await db.execute(
            select(
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
            ).where(device_filter)
        )
        device_row = device_result.one()

        # Daily device purchases
        daily_device_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
            )
            .where(device_filter)
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily_devices = [
            DailyDeviceItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
            )
            for row in daily_device_query
        ]

        return AddonsStatsResponse(
            total_purchases=totals.count,
            total_gb_purchased=totals.total_gb,
            addon_revenue_kopeks=addon_revenue,
            device_purchases=device_row.count,
            device_revenue_kopeks=device_row.revenue,
            by_package=by_package,
            daily=daily,
            daily_devices=daily_devices,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get addons stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load add-ons statistics',
        )


# ============ Deposits Schemas ============


class DepositByMethodItem(BaseModel):
    method: str
    count: int
    amount_kopeks: int


class DailyDepositItem(BaseModel):
    date: str
    count: int
    amount_kopeks: int


class DailyDepositByMethodItem(BaseModel):
    date: str
    method: str
    amount_kopeks: int


class DepositsStatsResponse(BaseModel):
    total_deposits: int
    total_amount_kopeks: int
    avg_deposit_kopeks: int
    by_method: list[DepositByMethodItem]
    daily: list[DailyDepositItem]
    daily_by_method: list[DailyDepositByMethodItem]


# ============ Deposits Endpoint ============


@router.get('/deposits', response_model=DepositsStatsResponse)
async def get_deposits_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> DepositsStatsResponse:
    """Get deposit statistics with payment method breakdown."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        base_filter = and_(
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed == True,
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            Transaction.created_at >= period_start,
            Transaction.created_at <= period_end,
        )

        totals_result = await db.execute(
            select(
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('amount'),
            ).where(base_filter)
        )
        totals = totals_result.one()
        total_deposits = totals.count
        total_amount = totals.amount
        avg_deposit = total_amount // total_deposits if total_deposits > 0 else 0

        by_method_query = await db.execute(
            select(
                Transaction.payment_method.label('method'),
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('amount'),
            )
            .where(base_filter)
            .group_by(Transaction.payment_method)
            .order_by(func.sum(Transaction.amount_kopeks).desc())
        )
        by_method = [
            DepositByMethodItem(method=row.method or 'unknown', count=row.count, amount_kopeks=row.amount)
            for row in by_method_query
        ]

        daily_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('amount'),
            )
            .where(base_filter)
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily = [
            DailyDepositItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
                amount_kopeks=row.amount,
            )
            for row in daily_query
        ]

        # Daily deposits grouped by payment method
        # base_filter already excludes NULLs via .in_(REAL_PAYMENT_METHODS), no coalesce needed
        daily_by_method_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                Transaction.payment_method.label('method'),
                func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('amount'),
            )
            .where(base_filter)
            .group_by(func.date(Transaction.created_at), Transaction.payment_method)
            .order_by(func.date(Transaction.created_at), Transaction.payment_method)
        )
        daily_by_method = [
            DailyDepositByMethodItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                method=row.method or 'unknown',
                amount_kopeks=row.amount,
            )
            for row in daily_by_method_query
        ]

        return DepositsStatsResponse(
            total_deposits=total_deposits,
            total_amount_kopeks=total_amount,
            avg_deposit_kopeks=avg_deposit,
            by_method=by_method,
            daily=daily,
            daily_by_method=daily_by_method,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get deposits stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load deposits statistics',
        )
