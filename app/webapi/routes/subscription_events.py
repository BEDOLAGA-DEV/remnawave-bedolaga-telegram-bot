from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.texts import get_texts
from app.database.crud.subscription_event import (
    create_subscription_event,
    list_subscription_events,
)
from app.database.models import Subscription, SubscriptionEvent, Transaction, User

from ..dependencies import get_db_session, require_api_token
from ..schemas.subscription_events import (
    SubscriptionEventCreate,
    SubscriptionEventListResponse,
    SubscriptionEventResponse,
)


router = APIRouter()


async def _get_user_or_error(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_SUBSCRIPTION_EVENTS_USER_NOT_FOUND',
                'User not found',
            ),
        )
    return user


async def _ensure_subscription_exists(db: AsyncSession, subscription_id: int | None) -> None:
    if not subscription_id:
        return

    subscription = await db.get(Subscription, subscription_id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_SUBSCRIPTION_EVENTS_SUBSCRIPTION_NOT_FOUND',
                'Subscription not found',
            ),
        )


async def _ensure_transaction_exists(db: AsyncSession, transaction_id: int | None) -> None:
    if not transaction_id:
        return

    transaction_exists = await db.scalar(select(Transaction.id).where(Transaction.id == transaction_id))
    if not transaction_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=get_texts('ru').t(
                'WEBAPI_SUBSCRIPTION_EVENTS_TRANSACTION_NOT_FOUND',
                'Transaction not found',
            ),
        )


def _serialize_event(event: SubscriptionEvent) -> SubscriptionEventResponse:
    user = event.user

    extra = event.extra or {}

    if event.event_type == 'promocode_activation':
        extra = {**extra}
        extra.setdefault('balance_before_kopeks', None)
        extra.setdefault('balance_after_kopeks', None)

    return SubscriptionEventResponse(
        id=event.id,
        event_type=event.event_type,
        user_id=event.user_id,
        user_full_name=user.full_name if user else '',
        user_username=user.username if user else None,
        user_telegram_id=user.telegram_id if user else 0,
        subscription_id=event.subscription_id,
        transaction_id=event.transaction_id,
        amount_kopeks=event.amount_kopeks,
        currency=event.currency,
        message=event.message,
        occurred_at=event.occurred_at,
        created_at=event.created_at,
        extra=extra,
    )


@router.post('', response_model=SubscriptionEventResponse, status_code=status.HTTP_201_CREATED)
async def receive_subscription_event(
    payload: SubscriptionEventCreate,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> SubscriptionEventResponse:
    user = await _get_user_or_error(db, payload.user_id)
    await _ensure_subscription_exists(db, payload.subscription_id)
    await _ensure_transaction_exists(db, payload.transaction_id)

    event = await create_subscription_event(
        db,
        user_id=payload.user_id,
        event_type=payload.event_type,
        subscription_id=payload.subscription_id,
        transaction_id=payload.transaction_id,
        amount_kopeks=payload.amount_kopeks,
        currency=payload.currency,
        message=payload.message,
        occurred_at=payload.occurred_at,
        extra=payload.extra or None,
    )

    await db.refresh(event, attribute_names=['user'])

    event.user = user

    return _serialize_event(event)


@router.get('', response_model=SubscriptionEventListResponse)
async def list_subscription_event_logs(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_types: Iterable[str] | None = Query(default=None, alias='event_type'),
    user_id: int | None = Query(default=None),
) -> SubscriptionEventListResponse:
    events, total = await list_subscription_events(
        db,
        limit=limit,
        offset=offset,
        event_types=event_types,
        user_id=user_id,
    )

    return SubscriptionEventListResponse(
        items=[_serialize_event(event) for event in events],
        total=total,
        limit=limit,
        offset=offset,
    )
