from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription import (
    create_paid_subscription,
    create_trial_subscription,
    deactivate_subscription,
    get_subscription_by_user_id,
    replace_subscription,
)
from app.database.crud.user import (
    add_user_balance,
    create_user,
    get_user_by_id,
    get_user_by_referral_code,
    get_user_by_telegram_id,
    update_user,
)
from app.database.models import PromoGroup, Subscription, User, UserStatus
from app.services.subscription_service import SubscriptionService

from ..dependencies import get_db_session, require_api_token
from ..schemas.users import (
    BalanceUpdateRequest,
    PromoGroupSummary,
    SubscriptionSummary,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserSubscriptionCreateRequest,
    UserUpdateRequest,
)


router = APIRouter()


def _serialize_promo_group(group: PromoGroup | None) -> PromoGroupSummary | None:
    if not group:
        return None
    return PromoGroupSummary(
        id=group.id,
        name=group.name,
        server_discount_percent=group.server_discount_percent,
        traffic_discount_percent=group.traffic_discount_percent,
        device_discount_percent=group.device_discount_percent,
        apply_discounts_to_addons=getattr(group, 'apply_discounts_to_addons', True),
    )


def _serialize_subscription(subscription: Subscription | None) -> SubscriptionSummary | None:
    if not subscription:
        return None

    return SubscriptionSummary(
        id=subscription.id,
        status=subscription.status,
        actual_status=subscription.actual_status,
        is_trial=subscription.is_trial,
        start_date=subscription.start_date,
        end_date=subscription.end_date,
        traffic_limit_gb=subscription.traffic_limit_gb,
        traffic_used_gb=subscription.traffic_used_gb,
        device_limit=subscription.device_limit,
        autopay_enabled=subscription.autopay_enabled,
        autopay_days_before=subscription.autopay_days_before,
        subscription_url=subscription.subscription_url,
        subscription_crypto_link=subscription.subscription_crypto_link,
        connected_squads=list(subscription.connected_squads or []),
    )


def _serialize_user(user: User) -> UserResponse:
    subscription = getattr(user, 'subscription', None)
    promo_group = getattr(user, 'promo_group', None)

    return UserResponse(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        status=user.status,
        language=user.language,
        balance_kopeks=user.balance_kopeks,
        balance_rubles=round(user.balance_kopeks / 100, 2),
        referral_code=user.referral_code,
        referred_by_id=user.referred_by_id,
        has_had_paid_subscription=user.has_had_paid_subscription,
        has_made_first_topup=user.has_made_first_topup,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_activity=user.last_activity,
        promo_group=_serialize_promo_group(promo_group),
        subscription=_serialize_subscription(subscription),
    )


def _apply_search_filter(query, search: str):
    search_lower = f'%{search.lower()}%'
    conditions = [
        func.lower(User.username).like(search_lower),
        func.lower(User.first_name).like(search_lower),
        func.lower(User.last_name).like(search_lower),
        func.lower(User.referral_code).like(search_lower),
    ]

    if search.isdigit():
        conditions.append(User.telegram_id == int(search))
        conditions.append(User.id == int(search))

    return query.where(or_(*conditions))


@router.get('', response_model=UserListResponse)
async def list_users(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: UserStatus | None = Query(default=None, alias='status'),
    promo_group_id: int | None = Query(default=None),
    search: str | None = Query(default=None),
) -> UserListResponse:
    base_query = select(User).options(
        selectinload(User.subscription),
        selectinload(User.promo_group),
    )

    if status_filter:
        base_query = base_query.where(User.status == status_filter.value)

    if promo_group_id:
        base_query = base_query.where(User.promo_group_id == promo_group_id)

    if search:
        base_query = _apply_search_filter(base_query, search)

    total_query = base_query.with_only_columns(func.count()).order_by(None)
    total = await db.scalar(total_query) or 0

    result = await db.execute(base_query.order_by(User.created_at.desc()).offset(offset).limit(limit))
    users = result.scalars().unique().all()

    return UserListResponse(
        items=[_serialize_user(user) for user in users],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get('/{user_id}', response_model=UserResponse)
async def get_user(
    user_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    # First check if the provided ID is a telegram_id
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        return _serialize_user(user)

    # If not found as telegram_id, check as internal user ID
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    return _serialize_user(user)


@router.get('/by-telegram-id/{telegram_id}', response_model=UserResponse)
async def get_user_by_telegram_id_endpoint(
    telegram_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """
    Get user by Telegram ID
    """
    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    return _serialize_user(user)


@router.post('', response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(
    payload: UserCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    # Check for duplicate telegram_id only if provided (skip for email-only users)
    if payload.telegram_id is not None:
        existing = await get_user_by_telegram_id(db, payload.telegram_id)
        if existing:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'User with this telegram_id already exists')

    user = await create_user(
        db,
        telegram_id=payload.telegram_id,
        username=payload.username,
        first_name=payload.first_name,
        last_name=payload.last_name,
        language=payload.language,
        referred_by_id=payload.referred_by_id,
    )

    if payload.promo_group_id and payload.promo_group_id != user.promo_group_id:
        promo_group = await get_promo_group_by_id(db, payload.promo_group_id)
        if not promo_group:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Promo group not found')
        user = await update_user(db, user, promo_group_id=promo_group.id)

    user = await get_user_by_id(db, user.id)
    return _serialize_user(user)


@router.patch('/{user_id}', response_model=UserResponse)
async def update_user_endpoint(
    user_id: int,
    payload: UserUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    # First check if the provided ID is a telegram_id
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        found_user = user
    else:
        # If not found as telegram_id, check as internal user ID
        found_user = await get_user_by_id(db, user_id)

    if not found_user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    updates: dict[str, Any] = {}

    if payload.username is not None:
        updates['username'] = payload.username
    if payload.first_name is not None:
        updates['first_name'] = payload.first_name
    if payload.last_name is not None:
        updates['last_name'] = payload.last_name
    if payload.language is not None:
        updates['language'] = payload.language
    if payload.has_had_paid_subscription is not None:
        updates['has_had_paid_subscription'] = payload.has_had_paid_subscription
    if payload.has_made_first_topup is not None:
        updates['has_made_first_topup'] = payload.has_made_first_topup

    if payload.status is not None:
        try:
            status_value = UserStatus(payload.status).value
        except ValueError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Invalid status') from error
        updates['status'] = status_value

    if payload.promo_group_id is not None:
        promo_group = await get_promo_group_by_id(db, payload.promo_group_id)
        if not promo_group:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Promo group not found')
        updates['promo_group_id'] = promo_group.id

    if payload.referral_code is not None and payload.referral_code != found_user.referral_code:
        existing_code_owner = await get_user_by_referral_code(db, payload.referral_code)
        if existing_code_owner and existing_code_owner.id != found_user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Referral code already in use')
        updates['referral_code'] = payload.referral_code

    if not updates:
        return _serialize_user(found_user)

    found_user = await update_user(db, found_user, **updates)
    # Reload the user to ensure we have the latest data
    if found_user.telegram_id == user_id:
        found_user = await get_user_by_telegram_id(db, user_id)
    else:
        found_user = await get_user_by_id(db, found_user.id)

    return _serialize_user(found_user)


@router.post('/{user_id}/balance', response_model=UserResponse)
async def update_balance(
    user_id: int,
    payload: BalanceUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    if payload.amount_kopeks == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Amount must be non-zero')

    # First check if the provided ID is a telegram_id
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        found_user = user
    else:
        # If not found as telegram_id, check as internal user ID
        found_user = await get_user_by_id(db, user_id)

    if not found_user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')

    success = await add_user_balance(
        db,
        found_user,
        amount_kopeks=payload.amount_kopeks,
        description=payload.description or 'Корректировка через веб-API',
        create_transaction=payload.create_transaction,
    )

    if not success:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, 'Failed to update balance')

    # Reload the user to ensure we have the latest data
    if found_user.telegram_id == user_id:
        found_user = await get_user_by_telegram_id(db, user_id)
    else:
        found_user = await get_user_by_id(db, found_user.id)

    return _serialize_user(found_user)


async def _get_user_by_id_or_telegram_id(db: AsyncSession, user_id: int) -> User:
    """Helper function to get user by ID or telegram_id"""
    user = await get_user_by_telegram_id(db, user_id)
    if user:
        return user

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')
    return user


@router.post('/{user_id}/subscription', response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_subscription(
    user_id: int,
    payload: UserSubscriptionCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """
    Создать или заменить подписку для пользователя.
    Поддерживает создание как триальных, так и платных подписок.
    """
    user = await _get_user_by_id_or_telegram_id(db, user_id)

    existing = await get_subscription_by_user_id(db, user.id)
    if existing and not payload.replace_existing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, 'User already has a subscription. Use replace_existing=true to replace it'
        )

    forced_devices = None
    if not settings.is_devices_selection_enabled():
        forced_devices = settings.get_disabled_mode_device_limit()

    if payload.is_trial:
        trial_device_limit = payload.device_limit
        if trial_device_limit is None:
            trial_device_limit = forced_devices
        duration_days = payload.duration_days or settings.TRIAL_DURATION_DAYS
        traffic_limit_gb = payload.traffic_limit_gb or settings.TRIAL_TRAFFIC_LIMIT_GB

        if existing:
            # Сохраняем существующие сквады при замене
            connected_squads = list(existing.connected_squads or [])
            if payload.squad_uuid:
                connected_squads = [payload.squad_uuid]
            elif payload.connected_squads:
                connected_squads = payload.connected_squads

            subscription = await replace_subscription(
                db,
                existing,
                duration_days=duration_days,
                traffic_limit_gb=traffic_limit_gb,
                device_limit=(trial_device_limit if trial_device_limit is not None else settings.TRIAL_DEVICE_LIMIT),
                connected_squads=connected_squads,
                is_trial=True,
                update_server_counters=True,
            )
        else:
            subscription = await create_trial_subscription(
                db,
                user_id=user.id,
                duration_days=duration_days,
                traffic_limit_gb=traffic_limit_gb,
                device_limit=trial_device_limit,
                squad_uuid=payload.squad_uuid,
            )
    else:
        if payload.duration_days is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'duration_days is required for paid subscriptions')
        device_limit = payload.device_limit
        if device_limit is None:
            if forced_devices is not None:
                device_limit = forced_devices
            else:
                device_limit = settings.DEFAULT_DEVICE_LIMIT

        if existing:
            subscription = await replace_subscription(
                db,
                existing,
                duration_days=payload.duration_days,
                traffic_limit_gb=payload.traffic_limit_gb or settings.DEFAULT_TRAFFIC_LIMIT_GB,
                device_limit=device_limit,
                connected_squads=payload.connected_squads or [],
                is_trial=False,
                update_server_counters=True,
            )
        else:
            subscription = await create_paid_subscription(
                db,
                user_id=user.id,
                duration_days=payload.duration_days,
                traffic_limit_gb=payload.traffic_limit_gb or settings.DEFAULT_TRAFFIC_LIMIT_GB,
                device_limit=device_limit,
                connected_squads=payload.connected_squads or [],
                update_server_counters=True,
            )

        # Создаем пользователя в RemnaWave для платных подписок
        subscription_service = SubscriptionService()
        await subscription_service.create_remnawave_user(db, subscription)

    # Перезагружаем пользователя с подпиской
    user = await get_user_by_id(db, user.id)
    return _serialize_user(user)


@router.delete('/{user_id}/subscription', response_model=UserResponse)
async def delete_user_subscription(
    user_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """
    Деактивировать подписку пользователя.
    Подписка не удаляется физически, а помечается как DISABLED.
    """
    user = await _get_user_by_id_or_telegram_id(db, user_id)

    subscription = await get_subscription_by_user_id(db, user.id)
    if not subscription:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User has no subscription')

    await deactivate_subscription(db, subscription)

    # Деактивируем пользователя в RemnaWave, если есть UUID
    if user.remnawave_uuid:
        subscription_service = SubscriptionService()
        await subscription_service.disable_remnawave_user(user.remnawave_uuid)

    # Перезагружаем пользователя
    user = await get_user_by_id(db, user.id)
    return _serialize_user(user)
