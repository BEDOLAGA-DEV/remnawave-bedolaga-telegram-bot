from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.localization.texts import get_texts
from app.database.crud.server_squad import get_random_trial_squad_uuid
from app.database.crud.subscription import (
    add_subscription_devices,
    add_subscription_squad,
    add_subscription_traffic,
    create_paid_subscription,
    create_trial_subscription,
    deactivate_subscription,
    extend_subscription,
    get_subscription_by_user_id,
    remove_subscription_squad,
    replace_subscription,
)
from app.database.models import Subscription, SubscriptionStatus
from app.services.subscription_service import SubscriptionService

from ..dependencies import get_db_session, require_api_token
from ..schemas.subscriptions import (
    SubscriptionCreateRequest,
    SubscriptionDevicesRequest,
    SubscriptionExtendRequest,
    SubscriptionResponse,
    SubscriptionSquadRequest,
    SubscriptionTrafficRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter()


def _serialize_subscription(subscription: Subscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        id=subscription.id,
        user_id=subscription.user_id,
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
        created_at=subscription.created_at,
        updated_at=subscription.updated_at,
    )


async def _choose_trial_squads(
    db: AsyncSession, requested_squad_uuid: str | None, fallback_squads: list[str]
) -> list[str]:
    if requested_squad_uuid:
        return [requested_squad_uuid]

    if fallback_squads:
        return fallback_squads

    try:
        squad_uuid = await get_random_trial_squad_uuid(db)
    except Exception as error:
        logger.error('Failed to select trial squad', error=error)
        squad_uuid = None

    if not squad_uuid:
        return []

    logger.debug('Selected trial squad for subscription replacement', squad_uuid=squad_uuid)
    return [squad_uuid]


async def _get_subscription(db: AsyncSession, subscription_id: int) -> Subscription:
    result = await db.execute(
        select(Subscription).options(selectinload(Subscription.user)).where(Subscription.id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t('WEBAPI_SUBSCRIPTIONS_NOT_FOUND', 'Subscription not found'),
        )
    return subscription


@router.get('', response_model=list[SubscriptionResponse])
async def list_subscriptions(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: SubscriptionStatus | None = Query(default=None, alias='status'),
    user_id: int | None = Query(default=None),
    is_trial: bool | None = Query(default=None),
) -> list[SubscriptionResponse]:
    query = select(Subscription).options(selectinload(Subscription.user))

    if status_filter:
        query = query.where(Subscription.status == status_filter.value)
    if user_id:
        query = query.where(Subscription.user_id == user_id)
    if is_trial is not None:
        query = query.where(Subscription.is_trial.is_(is_trial))

    query = query.order_by(Subscription.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    subscriptions = result.scalars().all()
    return [_serialize_subscription(sub) for sub in subscriptions]


@router.get('/{subscription_id}', response_model=SubscriptionResponse)
async def get_subscription(
    subscription_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    subscription = await _get_subscription(db, subscription_id)
    return _serialize_subscription(subscription)


@router.post('', response_model=SubscriptionResponse, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    payload: SubscriptionCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    existing = await get_subscription_by_user_id(db, payload.user_id)
    if existing and not payload.replace_existing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_SUBSCRIPTIONS_USER_ALREADY_HAS_A_SUBSCRIPTION',
                'User already has a subscription',
            ),
        )

    forced_devices = None
    if not settings.is_devices_selection_enabled():
        forced_devices = settings.get_disabled_mode_device_limit()

    subscription = None
    try:
        if payload.is_trial:
            trial_device_limit = payload.device_limit
            if trial_device_limit is None:
                trial_device_limit = forced_devices
            duration_days = payload.duration_days or settings.TRIAL_DURATION_DAYS
            traffic_limit_gb = payload.traffic_limit_gb or settings.TRIAL_TRAFFIC_LIMIT_GB

            if existing:
                connected_squads = await _choose_trial_squads(
                    db, payload.squad_uuid, list(existing.connected_squads or [])
                )
                subscription = await replace_subscription(
                    db,
                    existing,
                    duration_days=duration_days,
                    traffic_limit_gb=traffic_limit_gb,
                    device_limit=(
                        trial_device_limit if trial_device_limit is not None else settings.TRIAL_DEVICE_LIMIT
                    ),
                    connected_squads=connected_squads,
                    is_trial=True,
                    update_server_counters=True,
                )
            else:
                subscription = await create_trial_subscription(
                    db,
                    user_id=payload.user_id,
                    duration_days=duration_days,
                    traffic_limit_gb=traffic_limit_gb,
                    device_limit=trial_device_limit,
                    squad_uuid=payload.squad_uuid,
                )
        else:
            if payload.duration_days is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=get_texts('ru').t(
                        'WEBAPI_SUBSCRIPTIONS_DURATION_DAYS_IS_REQUIRED_FOR_PAID_SUBSCRIPTIONS',
                        'duration_days is required for paid subscriptions',
                    ),
                )
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
                    user_id=payload.user_id,
                    duration_days=payload.duration_days,
                    traffic_limit_gb=payload.traffic_limit_gb or settings.DEFAULT_TRAFFIC_LIMIT_GB,
                    device_limit=device_limit,
                    connected_squads=payload.connected_squads or [],
                    update_server_counters=True,
                )

        subscription_service = SubscriptionService()
        rem_user = await subscription_service.create_remnawave_user(db, subscription, reset_traffic=False)
        if not rem_user:
            raise ValueError('Failed to create/update user in Remnawave')

        await db.refresh(subscription)

    except HTTPException:
        raise
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            logger.exception('Rollback failed after error', e=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=get_texts('ru').t(
                'WEBAPI_SUBSCRIPTIONS_FAILED_TO_SYNC_WITH_REMNAWAVE',
                'Failed to sync with Remnawave: {error}',
            ).format(error=str(e)),
        )

    subscription = await _get_subscription(db, subscription.id)
    return _serialize_subscription(subscription)


@router.post('/{subscription_id}/extend', response_model=SubscriptionResponse)
async def extend_subscription_endpoint(
    subscription_id: int,
    payload: SubscriptionExtendRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    subscription = await _get_subscription(db, subscription_id)
    subscription = await extend_subscription(db, subscription, payload.days)
    subscription = await _get_subscription(db, subscription.id)
    return _serialize_subscription(subscription)


@router.post('/{subscription_id}/traffic', response_model=SubscriptionResponse)
async def add_subscription_traffic_endpoint(
    subscription_id: int,
    payload: SubscriptionTrafficRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    subscription = await _get_subscription(db, subscription_id)
    subscription = await add_subscription_traffic(db, subscription, payload.gb)
    subscription = await _get_subscription(db, subscription.id)
    return _serialize_subscription(subscription)


@router.post('/{subscription_id}/devices', response_model=SubscriptionResponse)
async def add_subscription_devices_endpoint(
    subscription_id: int,
    payload: SubscriptionDevicesRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    subscription = await _get_subscription(db, subscription_id)
    subscription = await add_subscription_devices(db, subscription, payload.devices)
    subscription = await _get_subscription(db, subscription.id)
    return _serialize_subscription(subscription)


@router.post('/{subscription_id}/squads', response_model=SubscriptionResponse)
async def add_subscription_squad_endpoint(
    subscription_id: int,
    payload: SubscriptionSquadRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    if not payload.squad_uuid:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=get_texts('ru').t(
                'WEBAPI_SUBSCRIPTIONS_SQUAD_UUID_IS_REQUIRED',
                'squad_uuid is required',
            ),
        )

    subscription = await _get_subscription(db, subscription_id)
    subscription = await add_subscription_squad(db, subscription, payload.squad_uuid)
    subscription = await _get_subscription(db, subscription.id)
    return _serialize_subscription(subscription)


@router.delete('/{subscription_id}/squads/{squad_uuid}', response_model=SubscriptionResponse)
async def remove_subscription_squad_endpoint(
    subscription_id: int,
    squad_uuid: str,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    subscription = await _get_subscription(db, subscription_id)
    subscription = await remove_subscription_squad(db, subscription, squad_uuid)
    subscription = await _get_subscription(db, subscription.id)
    return _serialize_subscription(subscription)


@router.delete('/{subscription_id}', response_model=SubscriptionResponse)
async def delete_subscription(
    subscription_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionResponse:
    """
    Деактивировать подписку.
    Подписка не удаляется физически, а помечается как DISABLED.
    Также деактивируется пользователь в RemnaWave, если есть UUID.
    """
    subscription = await _get_subscription(db, subscription_id)

    from app.database.crud.subscription import is_active_paid_subscription

    if is_active_paid_subscription(subscription):
        logger.info(
            '⏭️ Пропуск деактивации: у пользователя активная оплаченная подписка',
            subscription_id=subscription_id,
        )
        subscription = await _get_subscription(db, subscription.id)
        return _serialize_subscription(subscription)

    await deactivate_subscription(db, subscription)

    # Деактивируем пользователя в RemnaWave, если есть UUID
    if subscription.user and subscription.user.remnawave_uuid:
        subscription_service = SubscriptionService()
        await subscription_service.disable_remnawave_user(subscription.user.remnawave_uuid)

    subscription = await _get_subscription(db, subscription.id)
    return _serialize_subscription(subscription)
