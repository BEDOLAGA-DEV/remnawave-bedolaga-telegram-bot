"""Admin routes for managing users in cabinet."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Integer, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.campaign import get_campaign_registration_by_user
from app.database.crud.subscription import (
    extend_subscription,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.user import (
    add_user_balance,
    delete_user as soft_delete_user,
    get_referrals,
    get_user_by_id,
    get_user_by_telegram_id,
    get_users_count,
    get_users_list,
    get_users_spending_stats,
    get_users_statistics,
    subtract_user_balance,
)
from app.database.models import (
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    TrafficPurchase,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.utils.timezone import panel_datetime_to_naive_utc

from ..dependencies import get_cabinet_db, get_current_admin_user
from ..schemas.users import (
    DeleteDeviceResponse,
    DeleteUserRequest,
    DeleteUserResponse,
    DeviceInfo,
    DisableUserRequest,
    DisableUserResponse,
    FullDeleteUserRequest,
    FullDeleteUserResponse,
    PanelSyncStatusResponse,
    PanelUserInfo,
    PeriodPriceInfo,
    ResetDevicesResponse,
    ResetSubscriptionRequest,
    ResetSubscriptionResponse,
    ResetTrialRequest,
    ResetTrialResponse,
    SortByEnum,
    SyncFromPanelRequest,
    SyncFromPanelResponse,
    SyncToPanelRequest,
    SyncToPanelResponse,
    TrafficPurchaseItem,
    UpdateBalanceRequest,
    UpdateBalanceResponse,
    UpdatePromoGroupRequest,
    UpdatePromoGroupResponse,
    UpdateReferralCommissionRequest,
    UpdateReferralCommissionResponse,
    UpdateRestrictionsRequest,
    UpdateRestrictionsResponse,
    UpdateSubscriptionRequest,
    UpdateSubscriptionResponse,
    UpdateUserStatusRequest,
    UpdateUserStatusResponse,
    UserAvailableTariffItem,
    UserAvailableTariffsResponse,
    UserDetailResponse,
    UserDevicesResponse,
    UserListItem,
    UserNodeUsageItem,
    UserNodeUsageResponse,
    UserPanelInfoResponse,
    UserPromoGroupInfo,
    UserReferralInfo,
    UsersListResponse,
    UsersStatsResponse,
    UserStatusEnum,
    UserSubscriptionInfo,
    UserTransactionItem,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix='/admin/users', tags=['Cabinet Admin Users'])


def _build_user_list_item(user: User, spending_stats: dict = None) -> UserListItem:
    """Build UserListItem from User model."""
    stats = spending_stats or {}
    user_stats = stats.get(user.id, {'total_spent': 0, 'purchase_count': 0})

    subscription_status = None
    subscription_is_trial = False
    subscription_end_date = None
    has_subscription = False

    if user.subscription:
        has_subscription = True
        subscription_status = user.subscription.status
        subscription_is_trial = user.subscription.is_trial
        subscription_end_date = user.subscription.end_date

    return UserListItem(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        full_name=user.full_name,
        status=user.status,
        balance_kopeks=user.balance_kopeks,
        balance_rubles=user.balance_rubles,
        created_at=user.created_at,
        last_activity=user.last_activity,
        has_subscription=has_subscription,
        subscription_status=subscription_status,
        subscription_is_trial=subscription_is_trial,
        subscription_end_date=subscription_end_date,
        promo_group_id=user.promo_group_id,
        promo_group_name=user.promo_group.name if user.promo_group else None,
        total_spent_kopeks=user_stats.get('total_spent', 0),
        purchase_count=user_stats.get('purchase_count', 0),
        has_restrictions=user.has_restrictions,
        restriction_topup=user.restriction_topup,
        restriction_subscription=user.restriction_subscription,
    )


def _build_subscription_info(subscription: Subscription, tariff_name: str | None = None) -> UserSubscriptionInfo:
    """Build UserSubscriptionInfo from Subscription model."""
    days_remaining = 0
    is_active = False

    if subscription.end_date:
        delta = subscription.end_date - datetime.utcnow()
        days_remaining = max(0, delta.days)
        is_active = subscription.status == SubscriptionStatus.ACTIVE.value and subscription.end_date > datetime.utcnow()

    return UserSubscriptionInfo(
        id=subscription.id,
        status=subscription.status,
        is_trial=subscription.is_trial,
        start_date=subscription.start_date,
        end_date=subscription.end_date,
        traffic_limit_gb=subscription.traffic_limit_gb,
        traffic_used_gb=subscription.traffic_used_gb or 0.0,
        device_limit=subscription.device_limit,
        tariff_id=subscription.tariff_id,
        tariff_name=tariff_name,
        autopay_enabled=subscription.autopay_enabled,
        is_active=is_active,
        days_remaining=days_remaining,
    )


async def _build_subscription_info_async(db: AsyncSession, subscription: Subscription) -> UserSubscriptionInfo:
    """Build UserSubscriptionInfo from Subscription model, fetching tariff name and traffic purchases."""
    tariff_name = None
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        if tariff:
            tariff_name = tariff.name

    # Fetch traffic purchases
    now = datetime.utcnow()
    tp_query = (
        select(TrafficPurchase)
        .where(TrafficPurchase.subscription_id == subscription.id)
        .order_by(TrafficPurchase.created_at.desc())
    )
    tp_result = await db.execute(tp_query)
    purchases = tp_result.scalars().all()

    traffic_purchase_items = []
    for p in purchases:
        delta = p.expires_at - now
        days_remaining = max(0, delta.days)
        is_expired = now >= p.expires_at
        traffic_purchase_items.append(
            TrafficPurchaseItem(
                id=p.id,
                traffic_gb=p.traffic_gb,
                expires_at=p.expires_at,
                created_at=p.created_at,
                days_remaining=days_remaining,
                is_expired=is_expired,
            )
        )

    info = _build_subscription_info(subscription, tariff_name=tariff_name)
    info.purchased_traffic_gb = getattr(subscription, 'purchased_traffic_gb', 0) or 0
    info.traffic_purchases = traffic_purchase_items
    return info


async def _sync_subscription_to_panel(db: AsyncSession, user: User, subscription: Subscription) -> dict:
    """
    Sync user subscription to Remnawave panel.
    Creates user if not exists, updates if exists.
    Returns dict with changes/errors.
    """
    try:
        from app.config import settings
        from app.external.remnawave_api import TrafficLimitStrategy, UserStatus as PanelUserStatus
        from app.services.remnawave_service import RemnaWaveService
        from app.utils.subscription_utils import resolve_hwid_device_limit_for_payload

        service = RemnaWaveService()
        if not service.is_configured:
            logger.warning(f'Remnawave not configured, skipping panel sync for user {user.id}')
            return {'skipped': True, 'reason': 'Remnawave not configured'}

        is_active = (
            subscription.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
            and subscription.end_date
            and subscription.end_date > datetime.utcnow()
        )
        panel_status = PanelUserStatus.ACTIVE if is_active else PanelUserStatus.DISABLED

        expire_at = subscription.end_date
        if expire_at and expire_at <= datetime.utcnow():
            expire_at = datetime.utcnow() + timedelta(minutes=1)

        username = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )

        description = settings.format_remnawave_user_description(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )

        hwid_limit = resolve_hwid_device_limit_for_payload(subscription)
        traffic_limit_bytes = subscription.traffic_limit_gb * (1024**3) if subscription.traffic_limit_gb > 0 else 0

        changes = {}
        async with service.get_api_client() as api:
            panel_uuid = user.remnawave_uuid

            # Try to find existing user by UUID first
            if panel_uuid:
                existing_user = await api.get_user_by_uuid(panel_uuid)
                if not existing_user:
                    logger.warning(f'User {user.id} has stale remnawave_uuid {panel_uuid}, clearing')
                    panel_uuid = None
                    user.remnawave_uuid = None

            # Fallback: search by telegram_id
            if not panel_uuid and user.telegram_id:
                existing_users = await api.get_user_by_telegram_id(user.telegram_id)
                if existing_users:
                    panel_uuid = existing_users[0].uuid
                    user.remnawave_uuid = panel_uuid
                    changes['remnawave_uuid_discovered'] = panel_uuid

            # Fallback: search by email (for OAuth users without telegram_id)
            if not panel_uuid and user.email:
                existing_users = await api.get_user_by_email(user.email)
                if existing_users:
                    panel_uuid = existing_users[0].uuid
                    user.remnawave_uuid = panel_uuid
                    changes['remnawave_uuid_discovered'] = panel_uuid

            if panel_uuid:
                # Update existing user
                update_kwargs = {
                    'uuid': panel_uuid,
                    'status': panel_status,
                    'traffic_limit_bytes': traffic_limit_bytes,
                    'traffic_limit_strategy': TrafficLimitStrategy.MONTH,
                    'description': description,
                }
                if expire_at:
                    update_kwargs['expire_at'] = expire_at
                if subscription.connected_squads:
                    update_kwargs['active_internal_squads'] = subscription.connected_squads
                if hwid_limit is not None:
                    update_kwargs['hwid_device_limit'] = hwid_limit

                try:
                    await api.update_user(**update_kwargs)
                    changes['action'] = 'updated'
                    logger.info(f'Updated user {user.id} in Remnawave panel')
                except Exception as update_error:
                    if hasattr(update_error, 'status_code') and update_error.status_code == 404:
                        panel_uuid = None  # Will create new
                    else:
                        raise

            if not panel_uuid:
                # Create new user
                create_kwargs = {
                    'username': username,
                    'expire_at': expire_at or (datetime.utcnow() + timedelta(days=30)),
                    'status': panel_status,
                    'traffic_limit_bytes': traffic_limit_bytes,
                    'traffic_limit_strategy': TrafficLimitStrategy.MONTH,
                    'telegram_id': user.telegram_id,
                    'email': user.email,
                    'description': description,
                    'active_internal_squads': subscription.connected_squads or [],
                }
                if hwid_limit is not None:
                    create_kwargs['hwid_device_limit'] = hwid_limit

                new_panel_user = await api.create_user(**create_kwargs)
                user.remnawave_uuid = new_panel_user.uuid
                subscription.remnawave_short_uuid = new_panel_user.short_uuid
                subscription.subscription_url = new_panel_user.subscription_url
                changes['action'] = 'created'
                changes['panel_uuid'] = new_panel_user.uuid
                logger.info(f'Created user {user.id} in Remnawave panel: {new_panel_user.uuid}')

            user.last_remnawave_sync = datetime.utcnow()
            await db.commit()

        return changes

    except Exception as e:
        logger.error(f'Error syncing user {user.id} to panel: {e}')
        return {'error': str(e)}


# === List & Search ===


@router.get('', response_model=UsersListResponse)
async def list_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, max_length=255),
    email: str | None = Query(None, max_length=255),
    status: UserStatusEnum | None = Query(None),
    sort_by: SortByEnum = Query(SortByEnum.CREATED_AT),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Get paginated list of users with filtering and sorting.

    - **offset**: Pagination offset
    - **limit**: Number of users per page (max 200)
    - **search**: Search by telegram_id, username, first_name, last_name
    - **email**: Search by email
    - **status**: Filter by user status (active, blocked, deleted)
    - **sort_by**: Sort field (created_at, balance, traffic, last_activity, total_spent, purchase_count)
    """
    # Convert status enum to model enum
    user_status = None
    if status:
        user_status = UserStatus(status.value)

    # Map sort options
    order_by_balance = sort_by == SortByEnum.BALANCE
    order_by_traffic = sort_by == SortByEnum.TRAFFIC
    order_by_last_activity = sort_by == SortByEnum.LAST_ACTIVITY
    order_by_total_spent = sort_by == SortByEnum.TOTAL_SPENT
    order_by_purchase_count = sort_by == SortByEnum.PURCHASE_COUNT

    users = await get_users_list(
        db=db,
        offset=offset,
        limit=limit,
        search=search,
        email=email,
        status=user_status,
        order_by_balance=order_by_balance,
        order_by_traffic=order_by_traffic,
        order_by_last_activity=order_by_last_activity,
        order_by_total_spent=order_by_total_spent,
        order_by_purchase_count=order_by_purchase_count,
    )

    total = await get_users_count(db=db, status=user_status, search=search, email=email)

    # Get spending stats for all users
    user_ids = [u.id for u in users]
    spending_stats = await get_users_spending_stats(db, user_ids) if user_ids else {}

    items = [_build_user_list_item(u, spending_stats) for u in users]

    return UsersListResponse(
        users=items,
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get('/stats', response_model=UsersStatsResponse)
async def get_users_stats(
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get overall users statistics."""
    stats = await get_users_statistics(db)

    # Get subscription stats
    sub_stats_query = select(
        func.count(Subscription.id).label('total'),
        func.sum(
            func.cast(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > datetime.utcnow(),
                ),
                Integer,
            )
        ).label('active'),
        func.sum(func.cast(Subscription.is_trial == True, Integer)).label('trial'),
        func.sum(
            func.cast(
                or_(
                    Subscription.status == SubscriptionStatus.EXPIRED.value,
                    Subscription.end_date <= datetime.utcnow(),
                ),
                Integer,
            )
        ).label('expired'),
    )
    sub_result = await db.execute(sub_stats_query)
    sub_row = sub_result.one_or_none()

    users_with_subscription = sub_row.total or 0 if sub_row else 0
    users_with_active = sub_row.active or 0 if sub_row else 0
    users_with_trial = sub_row.trial or 0 if sub_row else 0
    users_with_expired = sub_row.expired or 0 if sub_row else 0

    # Get balance stats
    balance_query = select(
        func.sum(User.balance_kopeks).label('total'),
        func.avg(User.balance_kopeks).label('avg'),
    ).where(User.status == UserStatus.ACTIVE.value)
    balance_result = await db.execute(balance_query)
    balance_row = balance_result.one_or_none()
    total_balance = balance_row.total or 0 if balance_row else 0
    avg_balance = int(balance_row.avg or 0) if balance_row else 0

    # Get activity stats
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    active_today_q = select(func.count(User.id)).where(
        User.last_activity >= today_start,
        User.status == UserStatus.ACTIVE.value,
    )
    active_week_q = select(func.count(User.id)).where(
        User.last_activity >= week_ago,
        User.status == UserStatus.ACTIVE.value,
    )
    active_month_q = select(func.count(User.id)).where(
        User.last_activity >= month_ago,
        User.status == UserStatus.ACTIVE.value,
    )

    active_today = (await db.execute(active_today_q)).scalar() or 0
    active_week = (await db.execute(active_week_q)).scalar() or 0
    active_month = (await db.execute(active_month_q)).scalar() or 0

    # Count deleted users
    deleted_q = select(func.count(User.id)).where(User.status == UserStatus.DELETED.value)
    deleted_count = (await db.execute(deleted_q)).scalar() or 0

    return UsersStatsResponse(
        total_users=stats['total_users'],
        active_users=stats['active_users'],
        blocked_users=stats['blocked_users'],
        deleted_users=deleted_count,
        new_today=stats['new_today'],
        new_week=stats['new_week'],
        new_month=stats['new_month'],
        users_with_subscription=users_with_subscription,
        users_with_active_subscription=users_with_active,
        users_with_trial=users_with_trial,
        users_with_expired_subscription=users_with_expired,
        total_balance_kopeks=total_balance,
        total_balance_rubles=total_balance / 100,
        avg_balance_kopeks=avg_balance,
        active_today=active_today,
        active_week=active_week,
        active_month=active_month,
    )


# === User Detail ===


@router.get('/{user_id}', response_model=UserDetailResponse)
async def get_user_detail(
    user_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed user information by ID."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    # Get spending stats
    spending_stats = await get_users_spending_stats(db, [user.id])
    user_stats = spending_stats.get(user.id, {'total_spent': 0, 'purchase_count': 0})

    # Build subscription info
    subscription_info = None
    if user.subscription:
        subscription_info = await _build_subscription_info_async(db, user.subscription)

    # Build promo group info
    promo_group_info = None
    if user.promo_group:
        promo_group_info = UserPromoGroupInfo(
            id=user.promo_group.id,
            name=user.promo_group.name,
            is_default=user.promo_group.is_default,
        )

    # Get referrals count
    referrals = await get_referrals(db, user.id)
    referrals_count = len(referrals)

    # Calculate total referral earnings
    referral_earnings_q = select(func.sum(Transaction.amount_kopeks)).where(
        Transaction.user_id == user.id,
        Transaction.type == TransactionType.REFERRAL_REWARD.value,
        Transaction.is_completed == True,
    )
    referral_earnings = (await db.execute(referral_earnings_q)).scalar() or 0

    # Get referrer info
    referred_by_username = None
    if user.referred_by_id:
        referrer_q = select(User).where(User.id == user.referred_by_id)
        referrer_result = await db.execute(referrer_q)
        referrer = referrer_result.scalar_one_or_none()
        if referrer:
            referred_by_username = referrer.username or referrer.full_name

    referral_info = UserReferralInfo(
        referral_code=user.referral_code or '',
        referrals_count=referrals_count,
        total_earnings_kopeks=referral_earnings,
        commission_percent=user.referral_commission_percent,
        referred_by_id=user.referred_by_id,
        referred_by_username=referred_by_username,
    )

    # Get recent transactions
    transactions_q = (
        select(Transaction).where(Transaction.user_id == user.id).order_by(Transaction.created_at.desc()).limit(20)
    )
    transactions_result = await db.execute(transactions_q)
    transactions = transactions_result.scalars().all()

    recent_transactions = [
        UserTransactionItem(
            id=t.id,
            type=t.type,
            amount_kopeks=t.amount_kopeks,
            amount_rubles=t.amount_kopeks / 100,
            description=t.description,
            payment_method=t.payment_method,
            is_completed=t.is_completed,
            created_at=t.created_at,
        )
        for t in transactions
    ]

    # Get campaign info
    campaign_name = None
    campaign_id = None
    campaign_reg = await get_campaign_registration_by_user(db, user.id)
    if campaign_reg and campaign_reg.campaign:
        campaign_name = campaign_reg.campaign.name
        campaign_id = campaign_reg.campaign.id

    return UserDetailResponse(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        full_name=user.full_name,
        status=user.status,
        language=user.language,
        balance_kopeks=user.balance_kopeks,
        balance_rubles=user.balance_rubles,
        email=user.email,
        email_verified=user.email_verified,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_activity=user.last_activity,
        cabinet_last_login=user.cabinet_last_login,
        subscription=subscription_info,
        promo_group=promo_group_info,
        referral=referral_info,
        total_spent_kopeks=user_stats.get('total_spent', 0),
        purchase_count=user_stats.get('purchase_count', 0),
        used_promocodes=user.used_promocodes,
        has_had_paid_subscription=user.has_had_paid_subscription,
        lifetime_used_traffic_bytes=user.lifetime_used_traffic_bytes or 0,
        campaign_name=campaign_name,
        campaign_id=campaign_id,
        restriction_topup=user.restriction_topup,
        restriction_subscription=user.restriction_subscription,
        restriction_reason=user.restriction_reason,
        promo_offer_discount_percent=user.promo_offer_discount_percent,
        promo_offer_discount_source=user.promo_offer_discount_source,
        promo_offer_discount_expires_at=user.promo_offer_discount_expires_at,
        recent_transactions=recent_transactions,
        remnawave_uuid=user.remnawave_uuid,
    )


@router.get('/by-telegram/{telegram_id}', response_model=UserDetailResponse)
async def get_user_by_telegram(
    telegram_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user by Telegram ID."""
    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )
    return await get_user_detail(user.id, admin, db)


# === Panel Info ===


@router.get('/{user_id}/panel-info', response_model=UserPanelInfoResponse)
async def get_user_panel_info(
    user_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user panel info from Remnawave (config links, traffic, connection data)."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        if not service.is_configured:
            return UserPanelInfoResponse(found=False)

        async with service.get_api_client() as api:
            panel_user = None

            # Try by UUID first (works for all users including OAuth)
            if user.remnawave_uuid:
                panel_user = await api.get_user_by_uuid(user.remnawave_uuid)

            # Fallback: search by telegram_id
            if not panel_user and user.telegram_id:
                panel_users = await api.get_user_by_telegram_id(user.telegram_id)
                if panel_users:
                    panel_user = panel_users[0]

            # Fallback: search by email (OAuth users)
            if not panel_user and user.email:
                panel_users_by_email = await api.get_user_by_email(user.email)
                if panel_users_by_email:
                    panel_user = panel_users_by_email[0]

            if not panel_user:
                return UserPanelInfoResponse(found=False)

            # Resolve last connected node name via accessible nodes (lighter than get_all_nodes)
            last_node_name = None
            last_node_uuid = None
            if panel_user.user_traffic and panel_user.user_traffic.last_connected_node_uuid:
                last_node_uuid = panel_user.user_traffic.last_connected_node_uuid
                try:
                    accessible = await api.get_user_accessible_nodes(panel_user.uuid)
                    for node in accessible:
                        if node.uuid == last_node_uuid:
                            last_node_name = node.node_name
                            break
                except Exception:
                    logger.warning(f'Failed to resolve node name for user {user_id}')

            return UserPanelInfoResponse(
                found=True,
                trojan_password=panel_user.trojan_password,
                vless_uuid=panel_user.vless_uuid,
                ss_password=panel_user.ss_password,
                subscription_url=panel_user.subscription_url,
                happ_link=panel_user.happ_link,
                used_traffic_bytes=panel_user.used_traffic_bytes,
                lifetime_used_traffic_bytes=panel_user.lifetime_used_traffic_bytes,
                traffic_limit_bytes=panel_user.traffic_limit_bytes,
                first_connected_at=panel_user.first_connected_at,
                online_at=panel_user.online_at,
                last_connected_node_uuid=last_node_uuid,
                last_connected_node_name=last_node_name,
            )

    except Exception as e:
        logger.error(f'Error getting panel info for user {user_id}: {e}')
        return UserPanelInfoResponse(found=False)


@router.get('/{user_id}/node-usage', response_model=UserNodeUsageResponse)
async def get_user_node_usage(
    user_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user per-node traffic usage (always 30 days with daily breakdown)."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    if not user.remnawave_uuid:
        return UserNodeUsageResponse(items=[])

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        if not service.is_configured:
            return UserNodeUsageResponse(items=[])

        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=30)
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')

        async with service.get_api_client() as api:
            # Get user's accessible nodes (1 API call)
            accessible_nodes = await api.get_user_accessible_nodes(user.remnawave_uuid)

            # Get user bandwidth stats (1 API call)
            # Response: {categories: [dates], series: [{uuid, name, countryCode, total, data: [daily]}, ...]}
            stats = await api.get_bandwidth_stats_user(user.remnawave_uuid, start_str, end_str)

            categories: list[str] = []
            series_map: dict[str, dict] = {}
            if isinstance(stats, dict):
                categories = stats.get('categories', [])
                for s in stats.get('series', []):
                    series_map[s['uuid']] = {
                        'name': s.get('name', ''),
                        'country_code': s.get('countryCode', ''),
                        'total': int(s.get('total', 0)),
                        'daily': [int(v) for v in s.get('data', [])],
                    }

            # Build items: accessible nodes + any extra from stats
            items = []
            seen_uuids: set[str] = set()
            for node in accessible_nodes:
                seen_uuids.add(node.uuid)
                sr = series_map.get(node.uuid)
                items.append(
                    UserNodeUsageItem(
                        node_uuid=node.uuid,
                        node_name=sr['name'] if sr else node.node_name,
                        country_code=sr['country_code'] if sr else node.country_code,
                        total_bytes=sr['total'] if sr else 0,
                        daily_bytes=sr['daily'] if sr else [],
                    )
                )
            for nid, sr in series_map.items():
                if nid not in seen_uuids:
                    items.append(
                        UserNodeUsageItem(
                            node_uuid=nid,
                            node_name=sr['name'],
                            country_code=sr['country_code'],
                            total_bytes=sr['total'],
                            daily_bytes=sr['daily'],
                        )
                    )

            items.sort(key=lambda x: x.total_bytes, reverse=True)
            return UserNodeUsageResponse(items=items, categories=categories)

    except Exception as e:
        logger.error(f'Error getting node usage for user {user_id}: {e}')
        return UserNodeUsageResponse(items=[])


# === Balance Management ===


@router.post('/{user_id}/balance', response_model=UpdateBalanceResponse)
async def update_user_balance(
    user_id: int,
    request: UpdateBalanceRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Update user balance.

    - Positive amount: adds to balance
    - Negative amount: subtracts from balance
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    old_balance = user.balance_kopeks

    if request.amount_kopeks >= 0:
        # Add balance
        success = await add_user_balance(
            db=db,
            user=user,
            amount_kopeks=request.amount_kopeks,
            description=request.description,
            create_transaction=request.create_transaction,
            transaction_type=TransactionType.DEPOSIT,
        )
    else:
        # Subtract balance
        amount_to_subtract = abs(request.amount_kopeks)
        if user.balance_kopeks < amount_to_subtract:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Insufficient balance. Current: {user.balance_kopeks}, requested: {amount_to_subtract}',
            )
        success = await subtract_user_balance(
            db=db,
            user=user,
            amount_kopeks=amount_to_subtract,
            description=request.description,
            create_transaction=request.create_transaction,
        )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update balance',
        )

    # Refresh user
    await db.refresh(user)

    logger.info(
        f'Admin {admin.id} updated balance for user {user_id}: '
        f'{old_balance} -> {user.balance_kopeks} ({request.amount_kopeks:+d})'
    )

    return UpdateBalanceResponse(
        success=True,
        old_balance_kopeks=old_balance,
        new_balance_kopeks=user.balance_kopeks,
        message=f'Balance updated: {old_balance / 100:.2f}₽ -> {user.balance_kopeks / 100:.2f}₽',
    )


# === Subscription Management ===


@router.post('/{user_id}/subscription', response_model=UpdateSubscriptionResponse)
async def update_user_subscription(
    user_id: int,
    request: UpdateSubscriptionRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Update user subscription.

    Actions:
    - **extend**: Extend subscription by X days
    - **set_end_date**: Set specific end date
    - **change_tariff**: Change subscription tariff
    - **set_traffic**: Set traffic limit and/or used traffic
    - **toggle_autopay**: Enable/disable autopay
    - **cancel**: Cancel subscription (set status to expired)
    - **activate**: Activate subscription
    - **create**: Create new subscription if not exists
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    subscription = user.subscription

    if request.action == 'create':
        # Create new subscription
        if subscription:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='User already has a subscription',
            )

        from app.database.crud.subscription import create_paid_subscription

        days = request.days or 30
        is_trial = request.is_trial or False
        traffic_limit = request.traffic_limit_gb or 100
        device_limit = request.device_limit or 1
        connected_squads = []

        # Get tariff for settings if provided
        if request.tariff_id:
            tariff = await get_tariff_by_id(db, request.tariff_id)
            if tariff:
                if not request.traffic_limit_gb:
                    traffic_limit = tariff.traffic_limit_gb
                if not request.device_limit:
                    device_limit = tariff.device_limit
                if tariff.allowed_squads:
                    connected_squads = tariff.allowed_squads

        new_sub = await create_paid_subscription(
            db=db,
            user_id=user.id,
            duration_days=days,
            traffic_limit_gb=traffic_limit,
            device_limit=device_limit,
            is_trial=is_trial,
            tariff_id=request.tariff_id,
            connected_squads=connected_squads,
        )

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, new_sub)

        logger.info(f'Admin {admin.id} created subscription for user {user_id}')

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Subscription created for {days} days',
            subscription=await _build_subscription_info_async(db, new_sub),
        )

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User has no subscription',
        )

    if request.action == 'extend':
        if not request.days:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Days parameter is required for extend action',
            )

        await extend_subscription(db, subscription, request.days)
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} extended subscription for user {user_id} by {request.days} days')

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Subscription extended by {request.days} days',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'set_end_date':
        if not request.end_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='end_date parameter is required',
            )

        subscription.end_date = request.end_date
        if request.end_date > datetime.utcnow():
            subscription.status = SubscriptionStatus.ACTIVE.value
        else:
            subscription.status = SubscriptionStatus.EXPIRED.value

        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} set end_date for user {user_id} subscription')

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Subscription end date set to {request.end_date.isoformat()}',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'change_tariff':
        if request.tariff_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='tariff_id parameter is required',
            )

        tariff = await get_tariff_by_id(db, request.tariff_id)
        if not tariff:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Tariff not found',
            )

        subscription.tariff_id = request.tariff_id
        subscription.traffic_limit_gb = tariff.traffic_limit_gb
        subscription.device_limit = tariff.device_limit
        # Set squads from tariff
        if tariff.allowed_squads:
            subscription.connected_squads = tariff.allowed_squads
        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} changed tariff for user {user_id} to {tariff.name}')

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Tariff changed to {tariff.name}',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'set_traffic':
        if request.traffic_limit_gb is not None:
            subscription.traffic_limit_gb = request.traffic_limit_gb

        if request.traffic_used_gb is not None:
            subscription.traffic_used_gb = request.traffic_used_gb

        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} updated traffic for user {user_id}')

        return UpdateSubscriptionResponse(
            success=True,
            message='Traffic settings updated',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'toggle_autopay':
        if request.autopay_enabled is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='autopay_enabled parameter is required',
            )

        subscription.autopay_enabled = request.autopay_enabled
        await db.commit()
        await db.refresh(subscription)

        state = 'enabled' if request.autopay_enabled else 'disabled'
        logger.info(f'Admin {admin.id} {state} autopay for user {user_id}')

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Autopay {state}',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'cancel':
        subscription.status = SubscriptionStatus.EXPIRED.value
        subscription.end_date = datetime.utcnow()
        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} cancelled subscription for user {user_id}')

        return UpdateSubscriptionResponse(
            success=True,
            message='Subscription cancelled',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'activate':
        subscription.status = SubscriptionStatus.ACTIVE.value
        if subscription.end_date and subscription.end_date <= datetime.utcnow():
            # Extend by 30 days if expired
            subscription.end_date = datetime.utcnow() + timedelta(days=30)
        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} activated subscription for user {user_id}')

        return UpdateSubscriptionResponse(
            success=True,
            message='Subscription activated',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'add_traffic':
        if not request.traffic_gb:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='traffic_gb parameter is required for add_traffic action',
            )

        from app.database.crud.subscription import add_subscription_traffic

        await add_subscription_traffic(db, subscription, request.traffic_gb)
        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} added {request.traffic_gb} GB traffic for user {user_id}')

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Added {request.traffic_gb} GB traffic (30 days)',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'remove_traffic':
        if not request.traffic_purchase_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='traffic_purchase_id parameter is required for remove_traffic action',
            )

        # Find the traffic purchase
        tp_query = select(TrafficPurchase).where(
            TrafficPurchase.id == request.traffic_purchase_id,
            TrafficPurchase.subscription_id == subscription.id,
        )
        tp_result = await db.execute(tp_query)
        traffic_purchase = tp_result.scalar_one_or_none()
        if not traffic_purchase:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Traffic purchase not found',
            )

        removed_gb = traffic_purchase.traffic_gb

        # Decrement counters
        subscription.traffic_limit_gb = max(0, subscription.traffic_limit_gb - removed_gb)
        current_purchased = getattr(subscription, 'purchased_traffic_gb', 0) or 0
        subscription.purchased_traffic_gb = max(0, current_purchased - removed_gb)

        # Delete the purchase record
        await db.delete(traffic_purchase)

        # Recalculate traffic_reset_at from remaining active purchases
        now = datetime.utcnow()
        remaining_query = select(TrafficPurchase).where(
            TrafficPurchase.subscription_id == subscription.id,
            TrafficPurchase.expires_at > now,
            TrafficPurchase.id != request.traffic_purchase_id,
        )
        remaining_result = await db.execute(remaining_query)
        remaining_purchases = remaining_result.scalars().all()

        if remaining_purchases:
            subscription.traffic_reset_at = min(p.expires_at for p in remaining_purchases)
        else:
            subscription.traffic_reset_at = None

        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(
            f'Admin {admin.id} removed traffic purchase {request.traffic_purchase_id} ({removed_gb} GB) for user {user_id}'
        )

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Removed {removed_gb} GB traffic package',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    if request.action == 'set_device_limit':
        if request.device_limit is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='device_limit parameter is required for set_device_limit action',
            )

        subscription.device_limit = request.device_limit
        await db.commit()
        await db.refresh(subscription)

        # Sync to Remnawave panel
        await _sync_subscription_to_panel(db, user, subscription)

        logger.info(f'Admin {admin.id} set device limit to {request.device_limit} for user {user_id}')

        return UpdateSubscriptionResponse(
            success=True,
            message=f'Device limit set to {request.device_limit}',
            subscription=await _build_subscription_info_async(db, subscription),
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f'Unknown action: {request.action}',
    )


# === Available Tariffs ===


@router.get('/{user_id}/available-tariffs', response_model=UserAvailableTariffsResponse)
async def get_user_available_tariffs(
    user_id: int,
    include_inactive: bool = Query(False, description='Include inactive tariffs'),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Get list of tariffs available for a specific user.

    Takes into account user's promo group to determine which tariffs are accessible.
    Shows all tariffs with availability flag.
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    # Get all tariffs
    from app.database.crud.tariff import get_all_tariffs

    tariffs = await get_all_tariffs(db, include_inactive=include_inactive)

    # Get current subscription tariff
    current_tariff_id = None
    current_tariff_name = None
    if user.subscription and user.subscription.tariff_id:
        current_tariff_id = user.subscription.tariff_id
        if user.subscription.tariff:
            current_tariff_name = user.subscription.tariff.name

    # Build tariff items
    tariff_items = []
    for tariff in tariffs:
        # Check if available for user's promo group
        is_available = tariff.is_available_for_promo_group(user.promo_group_id)
        requires_promo_group = bool(tariff.allowed_promo_groups)

        # Build period prices
        period_prices = []
        if tariff.period_prices:
            for days_str, price_kopeks in sorted(tariff.period_prices.items(), key=lambda x: int(x[0])):
                days = int(days_str)
                period_prices.append(
                    PeriodPriceInfo(
                        days=days,
                        price_kopeks=price_kopeks,
                        price_rubles=price_kopeks / 100,
                    )
                )

        tariff_items.append(
            UserAvailableTariffItem(
                id=tariff.id,
                name=tariff.name,
                description=tariff.description,
                is_active=tariff.is_active,
                is_trial_available=tariff.is_trial_available,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                tier_level=tariff.tier_level,
                display_order=tariff.display_order,
                period_prices=period_prices,
                is_daily=tariff.is_daily,
                daily_price_kopeks=tariff.daily_price_kopeks,
                custom_days_enabled=tariff.custom_days_enabled,
                price_per_day_kopeks=tariff.price_per_day_kopeks,
                min_days=tariff.min_days,
                max_days=tariff.max_days,
                device_price_kopeks=tariff.device_price_kopeks,
                max_device_limit=tariff.max_device_limit,
                traffic_topup_enabled=tariff.traffic_topup_enabled,
                traffic_topup_packages=tariff.traffic_topup_packages or {},
                max_topup_traffic_gb=tariff.max_topup_traffic_gb,
                is_available=is_available,
                requires_promo_group=requires_promo_group,
            )
        )

    # Sort by display_order, then by tier_level
    tariff_items.sort(key=lambda t: (t.display_order, t.tier_level))

    return UserAvailableTariffsResponse(
        user_id=user.id,
        promo_group_id=user.promo_group_id,
        promo_group_name=user.promo_group.name if user.promo_group else None,
        tariffs=tariff_items,
        total=len(tariff_items),
        current_tariff_id=current_tariff_id,
        current_tariff_name=current_tariff_name,
    )


# === Status Management ===


@router.post('/{user_id}/status', response_model=UpdateUserStatusResponse)
async def update_user_status(
    user_id: int,
    request: UpdateUserStatusRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update user status (active, blocked, deleted)."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    old_status = user.status
    new_status = request.status.value

    if old_status == new_status:
        return UpdateUserStatusResponse(
            success=True,
            old_status=old_status,
            new_status=new_status,
            message='Status unchanged',
        )

    user.status = new_status
    user.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user)

    action = f'{old_status} -> {new_status}'
    if request.reason:
        action += f' (reason: {request.reason})'

    logger.info(f'Admin {admin.id} changed status for user {user_id}: {action}')

    return UpdateUserStatusResponse(
        success=True,
        old_status=old_status,
        new_status=new_status,
        message=f'Status changed from {old_status} to {new_status}',
    )


@router.post('/{user_id}/block', response_model=UpdateUserStatusResponse)
async def block_user(
    user_id: int,
    reason: str | None = None,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Block a user (shortcut for status update)."""
    request = UpdateUserStatusRequest(status=UserStatusEnum.BLOCKED, reason=reason)
    return await update_user_status(user_id, request, admin, db)


@router.post('/{user_id}/unblock', response_model=UpdateUserStatusResponse)
async def unblock_user(
    user_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Unblock a user (shortcut for status update)."""
    request = UpdateUserStatusRequest(status=UserStatusEnum.ACTIVE)
    return await update_user_status(user_id, request, admin, db)


# === Restrictions Management ===


@router.post('/{user_id}/restrictions', response_model=UpdateRestrictionsResponse)
async def update_user_restrictions(
    user_id: int,
    request: UpdateRestrictionsRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update user restrictions (topup, subscription)."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    if request.restriction_topup is not None:
        user.restriction_topup = request.restriction_topup

    if request.restriction_subscription is not None:
        user.restriction_subscription = request.restriction_subscription

    if request.restriction_reason is not None:
        user.restriction_reason = request.restriction_reason

    user.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user)

    logger.info(
        f'Admin {admin.id} updated restrictions for user {user_id}: '
        f'topup={user.restriction_topup}, subscription={user.restriction_subscription}'
    )

    return UpdateRestrictionsResponse(
        success=True,
        restriction_topup=user.restriction_topup,
        restriction_subscription=user.restriction_subscription,
        restriction_reason=user.restriction_reason,
        message='Restrictions updated',
    )


# === Promo Group Management ===


@router.post('/{user_id}/promo-group', response_model=UpdatePromoGroupResponse)
async def update_user_promo_group(
    user_id: int,
    request: UpdatePromoGroupRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update user promo group."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    old_promo_group_id = user.promo_group_id
    new_promo_group_id = request.promo_group_id
    promo_group_name = None

    if new_promo_group_id is not None:
        # Verify promo group exists
        result = await db.execute(select(PromoGroup).where(PromoGroup.id == new_promo_group_id))
        promo_group = result.scalar_one_or_none()
        if not promo_group:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Promo group not found',
            )
        promo_group_name = promo_group.name

    user.promo_group_id = new_promo_group_id
    user.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user)

    logger.info(
        f'Admin {admin.id} changed promo group for user {user_id}: {old_promo_group_id} -> {new_promo_group_id}'
    )

    return UpdatePromoGroupResponse(
        success=True,
        old_promo_group_id=old_promo_group_id,
        new_promo_group_id=new_promo_group_id,
        promo_group_name=promo_group_name,
        message='Promo group updated',
    )


# === Referral Commission ===


@router.post('/{user_id}/referral-commission', response_model=UpdateReferralCommissionResponse)
async def update_user_referral_commission(
    user_id: int,
    request: UpdateReferralCommissionRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update user's individual referral commission percentage."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    old_commission = user.referral_commission_percent
    user.referral_commission_percent = request.commission_percent
    user.updated_at = datetime.utcnow()
    await db.commit()

    logger.info(
        f'Admin {admin.id} changed referral commission for user {user_id}: {old_commission} -> {request.commission_percent}'
    )

    return UpdateReferralCommissionResponse(
        success=True,
        old_commission_percent=old_commission,
        new_commission_percent=request.commission_percent,
        message='Referral commission updated',
    )


# === Devices ===


@router.get('/{user_id}/devices', response_model=UserDevicesResponse)
async def get_user_devices(
    user_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user devices from Remnawave panel."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if not user.remnawave_uuid:
        return UserDevicesResponse()

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        if not service.is_configured:
            return UserDevicesResponse()

        async with service.get_api_client() as api:
            response = await api.get_user_devices(user.remnawave_uuid)

            devices = []
            for d in response.get('devices', []):
                hwid = d.get('hwid') or d.get('deviceId') or d.get('id')
                if not hwid:
                    continue
                devices.append(
                    DeviceInfo(
                        hwid=hwid,
                        platform=d.get('platform') or d.get('platformType') or '',
                        device_model=d.get('deviceModel') or d.get('model') or d.get('name') or '',
                        created_at=d.get('updatedAt') or d.get('lastSeen') or d.get('createdAt'),
                    )
                )

            device_limit = 0
            if user.subscription:
                device_limit = user.subscription.device_limit or 0

            return UserDevicesResponse(
                devices=devices,
                total=response.get('total', len(devices)),
                device_limit=device_limit,
            )

    except Exception as e:
        logger.error(f'Error fetching devices for user {user_id}: {e}')
        return UserDevicesResponse()


@router.delete('/{user_id}/devices/{hwid}', response_model=DeleteDeviceResponse)
async def delete_user_device(
    user_id: int,
    hwid: str,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a single device for user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if not user.remnawave_uuid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='User has no panel account')

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        async with service.get_api_client() as api:
            success = await api.remove_device(user.remnawave_uuid, hwid)

        if success:
            logger.info(f'Admin {admin.id} deleted device {hwid} for user {user_id}')
            return DeleteDeviceResponse(success=True, message='Device deleted', deleted_hwid=hwid)
        return DeleteDeviceResponse(success=False, message='Failed to delete device')

    except Exception as e:
        logger.error(f'Error deleting device {hwid} for user {user_id}: {e}')
        return DeleteDeviceResponse(success=False, message=str(e))


@router.delete('/{user_id}/devices', response_model=ResetDevicesResponse)
async def reset_user_devices(
    user_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reset all devices for user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')

    if not user.remnawave_uuid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='User has no panel account')

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        async with service.get_api_client() as api:
            devices_info = await api.get_user_devices(user.remnawave_uuid)
            devices = devices_info.get('devices', [])
            total = len(devices)

            if total == 0:
                return ResetDevicesResponse(success=True, message='No devices to reset', deleted_count=0)

            deleted = 0
            for d in devices:
                device_hwid = d.get('hwid') or d.get('deviceId') or d.get('id')
                if device_hwid:
                    try:
                        await api.remove_device(user.remnawave_uuid, device_hwid)
                        deleted += 1
                    except Exception:
                        pass

        logger.info(f'Admin {admin.id} reset devices for user {user_id}: {deleted}/{total}')
        return ResetDevicesResponse(success=True, message=f'Deleted {deleted}/{total} devices', deleted_count=deleted)

    except Exception as e:
        logger.error(f'Error resetting devices for user {user_id}: {e}')
        return ResetDevicesResponse(success=False, message=str(e))


# === Delete User ===


@router.delete('/{user_id}', response_model=DeleteUserResponse)
async def delete_user(
    user_id: int,
    request: DeleteUserRequest = DeleteUserRequest(),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Delete a user.

    - **soft_delete=True**: Mark user as deleted (default)
    - **soft_delete=False**: Permanently delete from database
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    if request.soft_delete:
        await soft_delete_user(db, user)
        action = 'soft deleted'
    else:
        # Hard delete
        await db.delete(user)
        await db.commit()
        action = 'permanently deleted'

    reason_text = f' (reason: {request.reason})' if request.reason else ''
    logger.info(f'Admin {admin.id} {action} user {user_id}{reason_text}')

    return DeleteUserResponse(
        success=True,
        message=f'User {action} successfully',
    )


@router.delete('/{user_id}/full', response_model=FullDeleteUserResponse)
async def full_delete_user(
    user_id: int,
    request: FullDeleteUserRequest = FullDeleteUserRequest(),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Full user deletion - removes from bot database AND Remnawave panel.

    Uses UserService.delete_user_account() which handles:
    - Deleting/disabling user in Remnawave panel
    - Removing all related records (payments, transactions, etc.)
    - Removing user from database
    """
    from app.services.user_service import UserService

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    panel_error: str | None = None
    deleted_from_panel = False

    # UserService.delete_user_account handles both bot DB and Remnawave panel
    user_service = UserService()
    success = await user_service.delete_user_account(db, user_id, admin.id)

    if success:
        deleted_from_panel = request.delete_from_panel and user.remnawave_uuid is not None

    reason_text = f' (reason: {request.reason})' if request.reason else ''
    logger.info(f'Admin {admin.id} fully deleted user {user_id}{reason_text}')

    return FullDeleteUserResponse(
        success=success,
        message='User fully deleted from bot and panel' if success else 'Failed to delete user',
        deleted_from_bot=success,
        deleted_from_panel=deleted_from_panel,
        panel_error=panel_error,
    )


@router.post('/{user_id}/reset-trial', response_model=ResetTrialResponse)
async def reset_user_trial(
    user_id: int,
    request: ResetTrialRequest = ResetTrialRequest(),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Reset user trial - allows user to activate trial again.

    Actions:
    - Delete current subscription if exists
    - Reset has_used_trial flag to False
    - User can now activate a new trial
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    subscription_deleted = False

    # Delete subscription if exists
    if user.subscription:
        # Deactivate in Remnawave panel first
        if user.remnawave_uuid:
            try:
                from app.services.subscription_service import SubscriptionService

                subscription_service = SubscriptionService()
                await subscription_service.disable_remnawave_user(user.remnawave_uuid)
                logger.info(f'Disabled Remnawave user {user.remnawave_uuid} for trial reset')
            except Exception as e:
                logger.warning(f'Failed to disable Remnawave user during trial reset: {e}')

        # Delete subscription from database
        from sqlalchemy import delete

        await db.execute(delete(Subscription).where(Subscription.user_id == user_id))
        subscription_deleted = True

    # Reset trial flag
    user.has_used_trial = False
    user.updated_at = datetime.utcnow()

    await db.commit()

    reason_text = f' (reason: {request.reason})' if request.reason else ''
    logger.info(f'Admin {admin.id} reset trial for user {user_id}{reason_text}')

    return ResetTrialResponse(
        success=True,
        message='Trial reset successfully. User can now activate a new trial.',
        subscription_deleted=subscription_deleted,
        has_used_trial_reset=True,
    )


@router.post('/{user_id}/reset-subscription', response_model=ResetSubscriptionResponse)
async def reset_user_subscription(
    user_id: int,
    request: ResetSubscriptionRequest = ResetSubscriptionRequest(),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Reset user subscription - removes/deactivates subscription.

    Actions:
    - Delete subscription from bot database
    - Optionally deactivate in Remnawave panel
    - User will have no active subscription
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    subscription_deleted = False
    panel_deactivated = False
    panel_error: str | None = None

    if not user.subscription:
        return ResetSubscriptionResponse(
            success=True,
            message='User has no subscription to reset',
            subscription_deleted=False,
            panel_deactivated=False,
        )

    # Deactivate in Remnawave panel if requested
    if request.deactivate_in_panel and user.remnawave_uuid:
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            panel_deactivated = await subscription_service.disable_remnawave_user(user.remnawave_uuid)
            if panel_deactivated:
                logger.info(f'Disabled Remnawave user {user.remnawave_uuid} for subscription reset')
        except Exception as e:
            panel_error = str(e)
            logger.warning(f'Failed to disable Remnawave user during subscription reset: {e}')

    # Delete subscription from database
    from sqlalchemy import delete

    await db.execute(delete(Subscription).where(Subscription.user_id == user_id))
    subscription_deleted = True

    user.updated_at = datetime.utcnow()
    await db.commit()

    reason_text = f' (reason: {request.reason})' if request.reason else ''
    logger.info(f'Admin {admin.id} reset subscription for user {user_id}{reason_text}')

    return ResetSubscriptionResponse(
        success=True,
        message='Subscription reset successfully',
        subscription_deleted=subscription_deleted,
        panel_deactivated=panel_deactivated,
        panel_error=panel_error,
    )


@router.post('/{user_id}/disable', response_model=DisableUserResponse)
async def disable_user(
    user_id: int,
    request: DisableUserRequest = DisableUserRequest(),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Disable user - deactivates subscription and blocks access.

    Actions:
    - Deactivate subscription in bot database
    - Deactivate in Remnawave panel
    - Block user account
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    subscription_deactivated = False
    panel_deactivated = False
    panel_error: str | None = None

    # Deactivate subscription in panel
    if user.remnawave_uuid:
        try:
            from app.services.subscription_service import SubscriptionService

            subscription_service = SubscriptionService()
            panel_deactivated = await subscription_service.disable_remnawave_user(user.remnawave_uuid)
            if panel_deactivated:
                logger.info(f'Disabled Remnawave user {user.remnawave_uuid}')
        except Exception as e:
            panel_error = str(e)
            logger.warning(f'Failed to disable Remnawave user: {e}')

    # Deactivate subscription in bot database
    if user.subscription:
        from app.database.crud.subscription import deactivate_subscription

        await deactivate_subscription(db, user.subscription)
        subscription_deactivated = True
        logger.info(f'Deactivated subscription for user {user_id}')

    # Block user account
    user.status = UserStatus.BLOCKED.value
    user.updated_at = datetime.utcnow()
    await db.commit()

    reason_text = f' (reason: {request.reason})' if request.reason else ''
    logger.info(f'Admin {admin.id} disabled user {user_id}{reason_text}')

    return DisableUserResponse(
        success=True,
        message='User disabled successfully',
        subscription_deactivated=subscription_deactivated,
        panel_deactivated=panel_deactivated,
        user_blocked=True,
        panel_error=panel_error,
    )


# === User Referrals ===


@router.get('/{user_id}/referrals', response_model=UsersListResponse)
async def get_user_referrals(
    user_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of users referred by this user."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    referrals = await get_referrals(db, user.id)

    # Apply pagination manually
    total = len(referrals)
    referrals = referrals[offset : offset + limit]

    # Get spending stats
    user_ids = [r.id for r in referrals]
    spending_stats = await get_users_spending_stats(db, user_ids) if user_ids else {}

    items = [_build_user_list_item(r, spending_stats) for r in referrals]

    return UsersListResponse(
        users=items,
        total=total,
        offset=offset,
        limit=limit,
    )


# === User Transactions ===


@router.get('/{user_id}/transactions')
async def get_user_transactions(
    user_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    transaction_type: str | None = Query(None),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get user transactions."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    query = select(Transaction).where(Transaction.user_id == user.id)

    if transaction_type:
        query = query.where(Transaction.type == transaction_type)

    # Get total count
    count_query = select(func.count(Transaction.id)).where(Transaction.user_id == user.id)
    if transaction_type:
        count_query = count_query.where(Transaction.type == transaction_type)
    total = (await db.execute(count_query)).scalar() or 0

    # Get transactions
    query = query.order_by(Transaction.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    transactions = result.scalars().all()

    items = [
        UserTransactionItem(
            id=t.id,
            type=t.type,
            amount_kopeks=t.amount_kopeks,
            amount_rubles=t.amount_kopeks / 100,
            description=t.description,
            payment_method=t.payment_method,
            is_completed=t.is_completed,
            created_at=t.created_at,
        )
        for t in transactions
    ]

    return {
        'transactions': items,
        'total': total,
        'offset': offset,
        'limit': limit,
    }


# === Panel Sync ===


@router.get('/{user_id}/sync/status', response_model=PanelSyncStatusResponse)
async def get_user_sync_status(
    user_id: int,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Get sync status between bot and panel for a user.

    Shows differences between bot data and panel data.
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    # Bot data
    bot_sub_status = None
    bot_sub_end_date = None
    bot_traffic_limit = 0
    bot_traffic_used = 0.0
    bot_device_limit = 0
    bot_squads: list[str] = []

    if user.subscription:
        bot_sub_status = user.subscription.status
        bot_sub_end_date = user.subscription.end_date
        bot_traffic_limit = user.subscription.traffic_limit_gb
        bot_traffic_used = user.subscription.traffic_used_gb or 0.0
        bot_device_limit = user.subscription.device_limit or 0
        bot_squads = user.subscription.connected_squads or []

    # Panel data
    panel_found = False
    panel_status = None
    panel_expire_at = None
    panel_traffic_limit = 0.0
    panel_traffic_used = 0.0
    panel_device_limit = 0
    panel_squads: list[str] = []
    differences = []

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        if service.is_configured:
            async with service.get_api_client() as api:
                panel_user = None

                # Try by UUID first (works for all users including OAuth)
                if user.remnawave_uuid:
                    panel_user = await api.get_user_by_uuid(user.remnawave_uuid)

                # Fallback: search by telegram_id
                if not panel_user and user.telegram_id:
                    panel_users = await api.get_user_by_telegram_id(user.telegram_id)
                    if panel_users:
                        panel_user = panel_users[0]

                # Fallback: search by email (OAuth users)
                if not panel_user and user.email:
                    panel_users_by_email = await api.get_user_by_email(user.email)
                    if panel_users_by_email:
                        panel_user = panel_users_by_email[0]

                if panel_user:
                    panel_found = True
                    panel_status = panel_user.status.value if panel_user.status else None
                    panel_expire_at = panel_user.expire_at
                    panel_traffic_limit = (
                        panel_user.traffic_limit_bytes / (1024**3) if panel_user.traffic_limit_bytes else 0
                    )
                    panel_traffic_used = (
                        panel_user.used_traffic_bytes / (1024**3) if panel_user.used_traffic_bytes else 0
                    )
                    panel_device_limit = panel_user.hwid_device_limit or 0
                    # Extract squad UUIDs from active_internal_squads
                    panel_squads = [
                        s.get('uuid', '') for s in (panel_user.active_internal_squads or []) if s.get('uuid')
                    ]

                    # Check differences
                    if bot_sub_status and panel_status:
                        bot_active = bot_sub_status in ('active', 'trial')
                        panel_active = panel_status.upper() == 'ACTIVE'
                        if bot_active != panel_active:
                            differences.append(f'Status: bot={bot_sub_status}, panel={panel_status}')

                    if bot_sub_end_date and panel_expire_at:
                        # Convert both to naive UTC for comparison
                        # Bot dates are stored as naive UTC
                        bot_end_utc = (
                            bot_sub_end_date.replace(tzinfo=None) if bot_sub_end_date.tzinfo else bot_sub_end_date
                        )
                        # Panel returns local time with misleading +00:00 offset
                        panel_end_utc = panel_datetime_to_naive_utc(panel_expire_at)

                        diff_seconds = abs((bot_end_utc - panel_end_utc).total_seconds())
                        # Allow for timezone offset (3 hours = MSK) and small sync delays
                        # If diff is ~3 hours (10800 sec) +/- 5 min, assume it's timezone issue
                        is_timezone_diff = abs(diff_seconds - 10800) < 300  # 3 hours +/- 5 min
                        if diff_seconds > 3600 and not is_timezone_diff:  # More than 1 hour and not timezone
                            differences.append(f'End date differs by {diff_seconds / 3600:.1f} hours')

                    if abs(bot_traffic_limit - panel_traffic_limit) > 1:
                        differences.append(
                            f'Traffic limit: bot={bot_traffic_limit}GB, panel={panel_traffic_limit:.1f}GB'
                        )

                    if abs(bot_traffic_used - panel_traffic_used) > 0.5:
                        differences.append(
                            f'Traffic used: bot={bot_traffic_used:.2f}GB, panel={panel_traffic_used:.2f}GB'
                        )

                    # Compare device limits
                    if bot_device_limit != panel_device_limit:
                        differences.append(f'Device limit: bot={bot_device_limit}, panel={panel_device_limit}')

                    # Compare squads
                    bot_squads_set = set(bot_squads) if bot_squads else set()
                    panel_squads_set = set(panel_squads) if panel_squads else set()
                    if bot_squads_set != panel_squads_set:
                        only_in_bot = bot_squads_set - panel_squads_set
                        only_in_panel = panel_squads_set - bot_squads_set
                        squad_diff_parts = []
                        if only_in_bot:
                            squad_diff_parts.append(f'only in bot: {len(only_in_bot)}')
                        if only_in_panel:
                            squad_diff_parts.append(f'only in panel: {len(only_in_panel)}')
                        differences.append(f'Squads mismatch ({", ".join(squad_diff_parts)})')

    except Exception as e:
        logger.warning(f'Failed to get panel data for user {user_id}: {e}')
        differences.append(f'Error fetching panel data: {e!s}')

    return PanelSyncStatusResponse(
        user_id=user.id,
        telegram_id=user.telegram_id,
        remnawave_uuid=user.remnawave_uuid,
        last_sync=user.last_remnawave_sync,
        bot_subscription_status=bot_sub_status,
        bot_subscription_end_date=bot_sub_end_date,
        bot_traffic_limit_gb=bot_traffic_limit,
        bot_traffic_used_gb=bot_traffic_used,
        bot_device_limit=bot_device_limit,
        bot_squads=bot_squads,
        panel_found=panel_found,
        panel_status=panel_status,
        panel_expire_at=panel_expire_at,
        panel_traffic_limit_gb=panel_traffic_limit,
        panel_traffic_used_gb=panel_traffic_used,
        panel_device_limit=panel_device_limit,
        panel_squads=panel_squads,
        has_differences=len(differences) > 0,
        differences=differences,
    )


@router.post('/{user_id}/sync/from-panel', response_model=SyncFromPanelResponse)
async def sync_user_from_panel(
    user_id: int,
    request: SyncFromPanelRequest = SyncFromPanelRequest(),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Sync user data FROM panel TO bot.

    Fetches user data from Remnawave panel and updates local database.
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        if not service.is_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=service.configuration_error or 'Remnawave API not configured',
            )

        changes = {}
        errors = []
        panel_info = None

        async with service.get_api_client() as api:
            # Find user in panel: UUID → telegram_id → email
            panel_user = None

            if user.remnawave_uuid:
                panel_user = await api.get_user_by_uuid(user.remnawave_uuid)

            if not panel_user and user.telegram_id:
                panel_users = await api.get_user_by_telegram_id(user.telegram_id)
                if panel_users:
                    panel_user = panel_users[0]

            if not panel_user and user.email:
                panel_users_by_email = await api.get_user_by_email(user.email)
                if panel_users_by_email:
                    panel_user = panel_users_by_email[0]

            if not panel_user:
                return SyncFromPanelResponse(
                    success=False,
                    message='User not found in panel',
                    errors=['No user found in Remnawave panel by UUID, telegram_id, or email'],
                )

            # Build panel info
            active_squads = []
            if hasattr(panel_user, 'active_internal_squads') and panel_user.active_internal_squads:
                for squad in panel_user.active_internal_squads:
                    if hasattr(squad, 'uuid'):
                        active_squads.append(squad.uuid)
                    elif isinstance(squad, str):
                        active_squads.append(squad)

            panel_info = PanelUserInfo(
                uuid=panel_user.uuid,
                short_uuid=panel_user.short_uuid,
                username=panel_user.username,
                status=panel_user.status.value if panel_user.status else None,
                expire_at=panel_datetime_to_naive_utc(panel_user.expire_at) if panel_user.expire_at else None,
                traffic_limit_gb=panel_user.traffic_limit_bytes / (1024**3) if panel_user.traffic_limit_bytes else 0,
                traffic_used_gb=panel_user.used_traffic_bytes / (1024**3) if panel_user.used_traffic_bytes else 0,
                device_limit=panel_user.hwid_device_limit or 1,
                subscription_url=panel_user.subscription_url,
                active_squads=active_squads,
            )

            # Update remnawave_uuid if different
            if user.remnawave_uuid != panel_user.uuid:
                changes['remnawave_uuid'] = {'old': user.remnawave_uuid, 'new': panel_user.uuid}
                user.remnawave_uuid = panel_user.uuid

            # Update subscription if requested
            if request.update_subscription and user.subscription:
                sub = user.subscription

                # Update end date (normalize timezone)
                if panel_user.expire_at:
                    # Panel returns local time with misleading +00:00 offset
                    panel_expire_utc = panel_datetime_to_naive_utc(panel_user.expire_at)

                    sub_end_naive = (
                        sub.end_date.replace(tzinfo=None) if sub.end_date and sub.end_date.tzinfo else sub.end_date
                    )
                    if sub_end_naive != panel_expire_utc:
                        changes['end_date'] = {
                            'old': sub.end_date.isoformat() if sub.end_date else None,
                            'new': panel_expire_utc.isoformat(),
                        }
                        sub.end_date = panel_expire_utc

                # Update status
                panel_status_str = panel_user.status.value if panel_user.status else 'DISABLED'
                now = datetime.utcnow()
                # Compare with normalized panel expire date
                panel_expire_for_check = panel_expire_utc if panel_user.expire_at else None
                if panel_status_str == 'ACTIVE' and panel_expire_for_check and panel_expire_for_check > now:
                    new_status = SubscriptionStatus.ACTIVE.value
                elif panel_expire_for_check and panel_expire_for_check <= now:
                    new_status = SubscriptionStatus.EXPIRED.value
                else:
                    new_status = SubscriptionStatus.DISABLED.value

                if sub.status != new_status:
                    changes['status'] = {'old': sub.status, 'new': new_status}
                    sub.status = new_status

                # Update traffic limit
                panel_traffic_limit = (
                    int(panel_user.traffic_limit_bytes / (1024**3)) if panel_user.traffic_limit_bytes else 0
                )
                if sub.traffic_limit_gb != panel_traffic_limit:
                    changes['traffic_limit_gb'] = {'old': sub.traffic_limit_gb, 'new': panel_traffic_limit}
                    sub.traffic_limit_gb = panel_traffic_limit

                # Update device limit
                panel_device_limit = panel_user.hwid_device_limit or 1
                if sub.device_limit != panel_device_limit:
                    changes['device_limit'] = {'old': sub.device_limit, 'new': panel_device_limit}
                    sub.device_limit = panel_device_limit

                # Update connected squads
                if active_squads and sub.connected_squads != active_squads:
                    changes['connected_squads'] = {'old': sub.connected_squads, 'new': active_squads}
                    sub.connected_squads = active_squads

                # Update subscription URL
                if panel_user.subscription_url and sub.subscription_url != panel_user.subscription_url:
                    changes['subscription_url'] = {'old': sub.subscription_url, 'new': panel_user.subscription_url}
                    sub.subscription_url = panel_user.subscription_url

                # Update short UUID
                if panel_user.short_uuid and sub.remnawave_short_uuid != panel_user.short_uuid:
                    changes['remnawave_short_uuid'] = {'old': sub.remnawave_short_uuid, 'new': panel_user.short_uuid}
                    sub.remnawave_short_uuid = panel_user.short_uuid

            # Update traffic usage if requested
            if request.update_traffic and user.subscription:
                panel_traffic_used = panel_user.used_traffic_bytes / (1024**3) if panel_user.used_traffic_bytes else 0
                if abs((user.subscription.traffic_used_gb or 0) - panel_traffic_used) > 0.01:
                    changes['traffic_used_gb'] = {'old': user.subscription.traffic_used_gb, 'new': panel_traffic_used}
                    user.subscription.traffic_used_gb = panel_traffic_used

            # Create subscription if missing but user exists in panel
            if request.create_if_missing and not user.subscription and panel_user.expire_at:
                from app.database.crud.subscription import create_paid_subscription

                panel_traffic_limit = (
                    int(panel_user.traffic_limit_bytes / (1024**3)) if panel_user.traffic_limit_bytes else 100
                )
                # Panel returns local time with misleading +00:00 offset
                panel_expire_naive = panel_datetime_to_naive_utc(panel_user.expire_at)
                days_remaining = max(1, (panel_expire_naive - datetime.utcnow()).days)

                new_sub = await create_paid_subscription(
                    db=db,
                    user_id=user.id,
                    duration_days=days_remaining,
                    traffic_limit_gb=panel_traffic_limit,
                    device_limit=panel_user.hwid_device_limit or 1,
                    connected_squads=active_squads,
                )
                new_sub.remnawave_short_uuid = panel_user.short_uuid
                new_sub.subscription_url = panel_user.subscription_url
                changes['subscription_created'] = True

            # Update last sync time
            user.last_remnawave_sync = datetime.utcnow()
            user.updated_at = datetime.utcnow()

            await db.commit()

        logger.info(f'Admin {admin.id} synced user {user_id} from panel. Changes: {list(changes.keys())}')

        return SyncFromPanelResponse(
            success=True,
            message=f'Synced {len(changes)} changes from panel' if changes else 'No changes needed',
            panel_user=panel_info,
            changes=changes,
            errors=errors,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error syncing user {user_id} from panel: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Sync error: {e!s}',
        )


@router.post('/{user_id}/sync/to-panel', response_model=SyncToPanelResponse)
async def sync_user_to_panel(
    user_id: int,
    request: SyncToPanelRequest = SyncToPanelRequest(),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """
    Sync user data FROM bot TO panel.

    Sends user/subscription data to Remnawave panel, creating or updating as needed.
    """
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User not found',
        )

    if not user.subscription:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='User has no subscription to sync',
        )

    try:
        from app.config import settings
        from app.external.remnawave_api import TrafficLimitStrategy, UserStatus as PanelUserStatus
        from app.services.remnawave_service import RemnaWaveService
        from app.utils.subscription_utils import resolve_hwid_device_limit_for_payload

        service = RemnaWaveService()
        if not service.is_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=service.configuration_error or 'Remnawave API not configured',
            )

        sub = user.subscription
        changes = {}
        errors = []
        action = 'no_changes'
        panel_uuid = user.remnawave_uuid

        # Prepare data for panel
        is_active = (
            sub.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
            and sub.end_date
            and sub.end_date > datetime.utcnow()
        )
        panel_status = PanelUserStatus.ACTIVE if is_active else PanelUserStatus.DISABLED

        # Ensure expire_at is in future for panel
        expire_at = sub.end_date
        if expire_at and expire_at <= datetime.utcnow():
            expire_at = datetime.utcnow() + timedelta(minutes=1)

        username = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )

        description = settings.format_remnawave_user_description(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )

        hwid_limit = resolve_hwid_device_limit_for_payload(sub)
        traffic_limit_bytes = sub.traffic_limit_gb * (1024**3) if sub.traffic_limit_gb > 0 else 0

        async with service.get_api_client() as api:
            # Validate existing UUID
            if panel_uuid:
                existing_user = await api.get_user_by_uuid(panel_uuid)
                if not existing_user:
                    logger.warning(f'User {user.id} has stale remnawave_uuid {panel_uuid}, clearing')
                    panel_uuid = None
                    user.remnawave_uuid = None

            # Fallback: search by telegram_id
            if not panel_uuid and user.telegram_id:
                existing_users = await api.get_user_by_telegram_id(user.telegram_id)
                if existing_users:
                    panel_uuid = existing_users[0].uuid
                    user.remnawave_uuid = panel_uuid
                    changes['remnawave_uuid_discovered'] = panel_uuid

            # Fallback: search by email (OAuth users)
            if not panel_uuid and user.email:
                existing_users = await api.get_user_by_email(user.email)
                if existing_users:
                    panel_uuid = existing_users[0].uuid
                    user.remnawave_uuid = panel_uuid
                    changes['remnawave_uuid_discovered'] = panel_uuid

            if panel_uuid:
                # Update existing user
                update_kwargs = {'uuid': panel_uuid}

                if request.update_status:
                    update_kwargs['status'] = panel_status
                    changes['status'] = panel_status.value

                if request.update_expire_date and expire_at:
                    update_kwargs['expire_at'] = expire_at
                    changes['expire_at'] = expire_at.isoformat()

                if request.update_traffic_limit:
                    update_kwargs['traffic_limit_bytes'] = traffic_limit_bytes
                    update_kwargs['traffic_limit_strategy'] = TrafficLimitStrategy.MONTH
                    changes['traffic_limit_gb'] = sub.traffic_limit_gb

                if request.update_squads and sub.connected_squads:
                    update_kwargs['active_internal_squads'] = sub.connected_squads
                    changes['connected_squads'] = sub.connected_squads

                update_kwargs['description'] = description
                if hwid_limit is not None:
                    update_kwargs['hwid_device_limit'] = hwid_limit
                    changes['device_limit'] = hwid_limit

                try:
                    await api.update_user(**update_kwargs)
                    action = 'updated'
                except Exception as update_error:
                    if hasattr(update_error, 'status_code') and update_error.status_code == 404:
                        # User not found in panel, create new
                        panel_uuid = None
                    else:
                        raise

            if not panel_uuid and request.create_if_missing:
                # Create new user in panel
                create_kwargs = {
                    'username': username,
                    'expire_at': expire_at or (datetime.utcnow() + timedelta(days=30)),
                    'status': panel_status,
                    'traffic_limit_bytes': traffic_limit_bytes,
                    'traffic_limit_strategy': TrafficLimitStrategy.MONTH,
                    'telegram_id': user.telegram_id,
                    'email': user.email,
                    'description': description,
                    'active_internal_squads': sub.connected_squads or [],
                }

                if hwid_limit is not None:
                    create_kwargs['hwid_device_limit'] = hwid_limit

                new_panel_user = await api.create_user(**create_kwargs)
                panel_uuid = new_panel_user.uuid
                user.remnawave_uuid = new_panel_user.uuid
                sub.remnawave_short_uuid = new_panel_user.short_uuid
                sub.subscription_url = new_panel_user.subscription_url

                changes['created_in_panel'] = True
                changes['panel_uuid'] = panel_uuid
                changes['short_uuid'] = new_panel_user.short_uuid
                action = 'created'

            # Update last sync time
            user.last_remnawave_sync = datetime.utcnow()
            user.updated_at = datetime.utcnow()

            await db.commit()

        logger.info(f'Admin {admin.id} synced user {user_id} to panel. Action: {action}')

        return SyncToPanelResponse(
            success=True,
            message=f'User {action} in panel' if action != 'no_changes' else 'No changes needed',
            action=action,
            panel_uuid=panel_uuid,
            changes=changes,
            errors=errors,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error syncing user {user_id} to panel: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Sync error: {e!s}',
        )
