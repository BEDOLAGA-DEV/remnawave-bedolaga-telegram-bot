"""Mobile cabinet-admin facade for Wave Machine integrations.

This router freezes the `bedolaga-mobile-cabinet-v1` backend contract. It is
intentionally narrow and never depends on the legacy root Web API token guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cabinet.routes.admin_settings import SettingUpdateRequest, bot_configuration_service, update_setting
from app.cabinet.routes.admin_tickets import (
    AdminReplyRequest,
    AdminStatusUpdateRequest,
    get_all_tickets,
    get_ticket_detail,
    reply_to_ticket,
    update_ticket_status,
)
from app.cabinet.routes.admin_users import (
    get_user_by_telegram,
    get_user_detail,
    list_users,
    reset_user_subscription,
    update_user_balance,
)
from app.cabinet.routes.media import MediaUploadResponse, upload_media
from app.cabinet.schemas.admin_mobile import (
    CONTRACT_VERSION,
    MobileContractInfoResponse,
    MobileDashboardStatsResponse,
    MobileDisabledFeatureResponse,
    MobileIncomeResponse,
    MobileSettingsCorsContractResponse,
    MobileSettingsCorsKey,
    MobileSubscriptionResponse,
    MobileTransactionItem,
    MobileTransactionListResponse,
)
from app.database.crud.referral import get_referral_statistics
from app.database.crud.subscription import get_subscriptions_statistics, get_trial_statistics
from app.database.crud.transaction import REAL_PAYMENT_METHODS, get_transactions_statistics
from app.database.crud.user import get_users_statistics
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    Ticket,
    TicketStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)

from ..dependencies import get_cabinet_db, require_mobile_admin_permission
from ..schemas.users import ResetSubscriptionRequest, SortByEnum, UpdateBalanceRequest, UserStatusEnum


router = APIRouter(prefix='/admin/mobile', tags=['Cabinet Admin Mobile'])

_CORS_KEYS = ('WEB_API_ALLOWED_ORIGINS', 'CABINET_ALLOWED_ORIGINS')


def _kopeks_to_rubles(value: float | None) -> float:
    return round((value or 0) / 100, 2)


async def _get_mobile_overview(db: AsyncSession) -> dict[str, object]:
    total_users = await db.scalar(select(func.count()).select_from(User)) or 0
    active_users = (
        await db.scalar(select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE.value)) or 0
    )
    blocked_users = (
        await db.scalar(select(func.count()).select_from(User).where(User.status == UserStatus.BLOCKED.value)) or 0
    )
    total_balance_kopeks = await db.scalar(select(func.coalesce(func.sum(User.balance_kopeks), 0))) or 0
    active_subscriptions = (
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.status == SubscriptionStatus.ACTIVE.value)
        )
        or 0
    )
    expired_subscriptions = (
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.status == SubscriptionStatus.EXPIRED.value)
        )
        or 0
    )
    pending_tickets = (
        await db.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.status.in_([TicketStatus.OPEN.value, TicketStatus.ANSWERED.value]))
        )
        or 0
    )
    today = datetime.now(UTC).date()
    today_transactions = (
        await db.scalar(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                func.date(Transaction.created_at) == today,
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            )
        )
        or 0
    )
    return {
        'users': {
            'total': total_users,
            'active': active_users,
            'blocked': blocked_users,
            'balance_kopeks': int(total_balance_kopeks),
            'balance_rubles': _kopeks_to_rubles(total_balance_kopeks),
        },
        'subscriptions': {
            'active': active_subscriptions,
            'expired': expired_subscriptions,
        },
        'support': {
            'open_tickets': pending_tickets,
        },
        'payments': {
            'today_kopeks': int(today_transactions),
            'today_rubles': _kopeks_to_rubles(today_transactions),
        },
    }


def _serialize_subscription(subscription: Subscription) -> MobileSubscriptionResponse:
    """Serialize a subscription without importing root Web API auth dependencies."""
    return MobileSubscriptionResponse(
        id=subscription.id,
        user_id=subscription.user_id,
        status=subscription.status,
        actual_status=subscription.actual_status,
        is_trial=subscription.is_trial,
        start_date=subscription.start_date,
        end_date=subscription.end_date,
        traffic_limit_gb=subscription.traffic_limit_gb,
        traffic_used_gb=subscription.traffic_used_gb or 0,
        device_limit=subscription.device_limit or 0,
        autopay_enabled=subscription.autopay_enabled,
        autopay_days_before=subscription.autopay_days_before,
        subscription_url=subscription.subscription_url,
        subscription_crypto_link=subscription.subscription_crypto_link,
        connected_squads=list(subscription.connected_squads or []),
        created_at=subscription.created_at,
        updated_at=subscription.updated_at,
    )


def _serialize_transaction(transaction: Transaction) -> MobileTransactionItem:
    return MobileTransactionItem(
        id=transaction.id,
        user_id=transaction.user_id,
        type=transaction.type,
        amount_kopeks=transaction.amount_kopeks,
        amount_rubles=round(transaction.amount_kopeks / 100, 2),
        description=transaction.description,
        payment_method=transaction.payment_method,
        external_id=transaction.external_id,
        is_completed=transaction.is_completed,
        created_at=transaction.created_at,
        completed_at=transaction.completed_at,
    )


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    if month < 1 or month > 12:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'month must be between 1 and 12')
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + int(month == 12), 1 if month == 12 else month + 1, 1, tzinfo=UTC)
    return start, end


@router.get('/contract', response_model=MobileContractInfoResponse)
async def get_mobile_contract(
    _: User = Depends(require_mobile_admin_permission('users:read')),
) -> MobileContractInfoResponse:
    """Return the frozen mobile cabinet contract metadata."""
    return MobileContractInfoResponse()


@router.get('/tickets')
async def get_mobile_tickets(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    status_filter: str | None = Query(None, alias='status', description='Filter by status'),
    priority_filter: str | None = Query(None, alias='priority', description='Filter by priority'),
    user_id: int | None = Query(None, description='Filter by user ID'),
    admin: User = Depends(require_mobile_admin_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for the admin ticket list."""
    return await get_all_tickets(
        page=page,
        per_page=per_page,
        status_filter=status_filter,
        priority_filter=priority_filter,
        user_id=user_id,
        admin=admin,
        db=db,
    )


@router.get('/tickets/{ticket_id}')
async def get_mobile_ticket_detail(
    ticket_id: int,
    admin: User = Depends(require_mobile_admin_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for admin ticket detail."""
    return await get_ticket_detail(ticket_id=ticket_id, admin=admin, db=db)


@router.post('/tickets/{ticket_id}/reply')
async def reply_to_mobile_ticket(
    ticket_id: int,
    request: AdminReplyRequest,
    admin: User = Depends(require_mobile_admin_permission('tickets:reply')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for replying to a ticket."""
    return await reply_to_ticket(ticket_id=ticket_id, request=request, admin=admin, db=db)


@router.post('/tickets/{ticket_id}/status')
async def update_mobile_ticket_status(
    ticket_id: int,
    request: AdminStatusUpdateRequest,
    admin: User = Depends(require_mobile_admin_permission('tickets:close')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for ticket status updates."""
    return await update_ticket_status(ticket_id=ticket_id, request=request, admin=admin, db=db)


@router.get('/users')
async def list_mobile_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, max_length=255),
    email: str | None = Query(None, max_length=255),
    status_filter: UserStatusEnum | None = Query(None, alias='status'),
    subscription_status: str | None = Query(None, max_length=20),
    tariff_id: str | None = Query(None, max_length=255),
    promo_group_id: int | None = Query(None),
    campaign_id: int | None = Query(None),
    partner_id: int | None = Query(None),
    sort_by: SortByEnum = Query(SortByEnum.CREATED_AT),
    admin: User = Depends(require_mobile_admin_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for user search/list."""
    return await list_users(
        offset=offset,
        limit=limit,
        search=search,
        email=email,
        status=status_filter,
        subscription_status=subscription_status,
        tariff_id=tariff_id,
        promo_group_id=promo_group_id,
        campaign_id=campaign_id,
        partner_id=partner_id,
        sort_by=sort_by,
        admin=admin,
        db=db,
    )


@router.get('/users/by-telegram/{telegram_id}')
async def get_mobile_user_by_telegram(
    telegram_id: int,
    admin: User = Depends(require_mobile_admin_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for Telegram ID user lookup."""
    return await get_user_by_telegram(telegram_id=telegram_id, admin=admin, db=db)


@router.get('/users/{user_id}')
async def get_mobile_user_detail(
    user_id: int,
    admin: User = Depends(require_mobile_admin_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for user detail."""
    return await get_user_detail(user_id=user_id, admin=admin, db=db)


@router.post('/users/{user_id}/balance')
async def update_mobile_user_balance(
    user_id: int,
    request: UpdateBalanceRequest,
    admin: User = Depends(require_mobile_admin_permission('users:balance')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Mobile role-gated wrapper for user balance updates."""
    return await update_user_balance(user_id=user_id, request=request, admin=admin, db=db)


@router.post('/media/upload', response_model=MediaUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_mobile_media(
    request: Request,
    admin: User = Depends(require_mobile_admin_permission('tickets:reply')),
    file: UploadFile = File(...),
    media_type: str = Form('photo', description='File type: photo, video, or document'),
) -> MediaUploadResponse:
    """Mobile role-gated media upload for ticket replies."""
    return await upload_media(request=request, user=admin, file=file, media_type=media_type)


@router.get('/subscriptions/lookup', response_model=MobileSubscriptionResponse)
async def lookup_subscription_by_url(
    subscription_url: str = Query(..., min_length=1, max_length=4096),
    _: User = Depends(require_mobile_admin_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MobileSubscriptionResponse:
    """Find a subscription by URL using cabinet JWT auth."""
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            or_(
                Subscription.subscription_url == subscription_url,
                Subscription.subscription_crypto_link == subscription_url,
            )
        )
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Subscription not found')
    return _serialize_subscription(subscription)


@router.get('/subscriptions/{subscription_id}', response_model=MobileSubscriptionResponse)
async def get_mobile_subscription(
    subscription_id: int,
    _: User = Depends(require_mobile_admin_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MobileSubscriptionResponse:
    """Return subscription detail by internal subscription ID."""
    result = await db.execute(
        select(Subscription).options(selectinload(Subscription.user)).where(Subscription.id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Subscription not found')
    return _serialize_subscription(subscription)


@router.get('/transactions/monthly-income', response_model=MobileIncomeResponse)
async def get_mobile_monthly_income(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    _: User = Depends(require_mobile_admin_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MobileIncomeResponse:
    """Return completed real-payment income for a calendar month."""
    period_start, period_end = _month_bounds(year, month)
    amount_expr = func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)
    count_expr = func.count(Transaction.id)
    result = await db.execute(
        select(amount_expr, count_expr).where(
            Transaction.created_at >= period_start,
            Transaction.created_at < period_end,
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed.is_(True),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )
    )
    income_kopeks, transaction_count = result.one()
    income_kopeks = int(income_kopeks or 0)
    return MobileIncomeResponse(
        period_start=period_start,
        period_end=period_end,
        income_kopeks=income_kopeks,
        income_rubles=round(income_kopeks / 100, 2),
        transaction_count=int(transaction_count or 0),
        payment_methods=sorted(REAL_PAYMENT_METHODS),
    )


@router.get('/users/{user_id}/transactions', response_model=MobileTransactionListResponse)
async def get_mobile_user_transactions(
    user_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _: User = Depends(require_mobile_admin_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MobileTransactionListResponse:
    """Return completed real-payment user transactions for mobile spending views."""
    filters = [
        Transaction.user_id == user_id,
        Transaction.type == TransactionType.DEPOSIT.value,
        Transaction.is_completed.is_(True),
        Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
    ]
    total = await db.scalar(select(func.count()).select_from(Transaction).where(and_(*filters))) or 0
    result = await db.execute(
        select(Transaction).where(and_(*filters)).order_by(Transaction.created_at.desc()).offset(offset).limit(limit)
    )
    transactions = result.scalars().all()
    return MobileTransactionListResponse(
        items=[_serialize_transaction(tx) for tx in transactions],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post('/users/{user_id}/reset-subscription')
async def reset_mobile_user_subscription(
    user_id: int,
    request: ResetSubscriptionRequest = ResetSubscriptionRequest(),
    admin: User = Depends(require_mobile_admin_permission('users:subscription')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Cabinet-JWT equivalent of the legacy mobile delete-subscription action."""
    response = await reset_user_subscription(user_id=user_id, request=request, admin=admin, db=db)
    if hasattr(response, 'model_dump'):
        data = response.model_dump()
    else:
        data = dict(response)
    return {'contract_version': CONTRACT_VERSION, **data}


@router.get('/stats/full', response_model=MobileDashboardStatsResponse)
async def get_mobile_stats_full(
    _: User = Depends(require_mobile_admin_permission('stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> MobileDashboardStatsResponse:
    """Cabinet-JWT replacement for legacy root `/stats/full` mobile consumers."""
    overview = await _get_mobile_overview(db)
    users_stats = await get_users_statistics(db)
    subscriptions_stats = await get_subscriptions_statistics(db)
    trial_stats = await get_trial_statistics(db)
    transactions_stats = await get_transactions_statistics(
        db, start_date=datetime(2020, 1, 1, tzinfo=UTC), end_date=datetime.now(UTC)
    )
    referral_stats = await get_referral_statistics(db)

    transactions_totals = transactions_stats.get('totals', {})
    transactions_today = transactions_stats.get('today', {})
    transactions_totals = {
        **transactions_totals,
        'income_rubles': _kopeks_to_rubles(transactions_totals.get('income_kopeks')),
        'expenses_rubles': _kopeks_to_rubles(transactions_totals.get('expenses_kopeks')),
        'profit_rubles': _kopeks_to_rubles(transactions_totals.get('profit_kopeks')),
        'subscription_income_kopeks': abs(transactions_totals.get('subscription_income_kopeks', 0)),
        'subscription_income_rubles': _kopeks_to_rubles(abs(transactions_totals.get('subscription_income_kopeks', 0))),
    }
    transactions_today = {
        **transactions_today,
        'income_rubles': _kopeks_to_rubles(transactions_today.get('income_kopeks')),
    }
    referral_stats = {
        **referral_stats,
        'total_paid_rubles': _kopeks_to_rubles(referral_stats.get('total_paid_kopeks')),
        'today_earnings_rubles': _kopeks_to_rubles(referral_stats.get('today_earnings_kopeks')),
        'week_earnings_rubles': _kopeks_to_rubles(referral_stats.get('week_earnings_kopeks')),
        'month_earnings_rubles': _kopeks_to_rubles(referral_stats.get('month_earnings_kopeks')),
    }
    return MobileDashboardStatsResponse(
        overview=overview,
        users=users_stats,
        subscriptions={**subscriptions_stats, 'trial_statistics': trial_stats},
        transactions={**transactions_stats, 'totals': transactions_totals, 'today': transactions_today},
        referrals=referral_stats,
    )


@router.get('/realtime', response_model=MobileDisabledFeatureResponse)
async def get_mobile_realtime_contract(
    _: User = Depends(require_mobile_admin_permission('tickets:read')),
) -> MobileDisabledFeatureResponse:
    """Make realtime disablement explicit for mobile v1."""
    return MobileDisabledFeatureResponse(
        feature='realtime_tickets',
        reason='Mobile v1 must not use /ws?api_key=... or /cabinet/ws?token=... query-token websockets.',
    )


@router.get('/settings/cors', response_model=MobileSettingsCorsContractResponse)
async def get_mobile_cors_contract(
    _: User = Depends(require_mobile_admin_permission('settings:read')),
) -> MobileSettingsCorsContractResponse:
    """Describe post-auth CORS settings exposure for mobile."""
    keys: list[MobileSettingsCorsKey] = []
    for key in _CORS_KEYS:
        try:
            bot_configuration_service.get_definition(key)
        except KeyError:
            keys.append(MobileSettingsCorsKey(key=key, mode='operator-guidance', env_locked=True))
            continue
        env_locked = bot_configuration_service.is_env_locked(key)
        keys.append(
            MobileSettingsCorsKey(
                key=key,
                mode='read-only/operator-guidance' if env_locked else 'editable-after-auth',
                env_locked=env_locked,
                secret=bot_configuration_service.is_secret_key(key),
            )
        )
    return MobileSettingsCorsContractResponse(
        pre_auth_behavior='local/operator-guidance-only',
        server_side_edit='allowed only after cabinet JWT, mobile role allowlist, settings:edit, and env-lock checks',
        allowed_keys=keys,
    )


@router.put('/settings/cors/{key}')
async def update_mobile_cors_setting(
    key: str,
    payload: SettingUpdateRequest,
    admin: User = Depends(require_mobile_admin_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Post-auth server-side edit for the approved CORS keys only."""
    if key not in _CORS_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'CORS setting is not part of the mobile contract')
    return await update_setting(key=key, payload=payload, admin=admin, db=db)
