from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Security, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.webhook import (
    create_webhook,
    delete_webhook,
    get_webhook_by_id,
    list_webhooks,
    record_webhook_delivery,
    update_webhook,
)
from app.database.models import Webhook, WebhookDelivery

from ..dependencies import get_db_session, require_api_token
from ..schemas.webhooks import (
    WebhookCreateRequest,
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookListResponse,
    WebhookResponse,
    WebhookStatsResponse,
    WebhookUpdateRequest,
)

router = APIRouter()


def _serialize_webhook(webhook: Webhook) -> WebhookResponse:
    return WebhookResponse(
        id=webhook.id,
        name=webhook.name,
        url=webhook.url,
        event_type=webhook.event_type,
        is_active=webhook.is_active,
        description=webhook.description,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
        last_triggered_at=webhook.last_triggered_at,
        failure_count=webhook.failure_count,
        success_count=webhook.success_count,
    )


def _serialize_delivery(delivery: WebhookDelivery) -> WebhookDeliveryResponse:
    return WebhookDeliveryResponse(
        id=delivery.id,
        webhook_id=delivery.webhook_id,
        event_type=delivery.event_type,
        payload=delivery.payload,
        response_status=delivery.response_status,
        response_body=delivery.response_body,
        status=delivery.status,
        error_message=delivery.error_message,
        attempt_number=delivery.attempt_number,
        created_at=delivery.created_at,
        delivered_at=delivery.delivered_at,
        next_retry_at=delivery.next_retry_at,
    )


@router.get("", response_model=WebhookListResponse)
async def list_webhooks_endpoint(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_type: Optional[str] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
) -> WebhookListResponse:
    """Список webhooks."""
    webhooks, total = await list_webhooks(
        db,
        event_type=event_type,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )

    return WebhookListResponse(
        items=[_serialize_webhook(webhook) for webhook in webhooks],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=WebhookStatsResponse)
async def get_webhook_stats(
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WebhookStatsResponse:
    """Статистика по webhooks."""
    total_webhooks = await db.scalar(select(func.count(Webhook.id))) or 0
    active_webhooks = await db.scalar(
        select(func.count(Webhook.id)).where(Webhook.is_active == True)
    ) or 0

    total_deliveries = await db.scalar(select(func.count(WebhookDelivery.id))) or 0
    successful_deliveries = await db.scalar(
        select(func.count(WebhookDelivery.id)).where(WebhookDelivery.status == "success")
    ) or 0
    failed_deliveries = await db.scalar(
        select(func.count(WebhookDelivery.id)).where(WebhookDelivery.status == "failed")
    ) or 0

    success_rate = (
        (successful_deliveries / total_deliveries * 100) if total_deliveries > 0 else 0.0
    )

    return WebhookStatsResponse(
        total_webhooks=int(total_webhooks),
        active_webhooks=int(active_webhooks),
        total_deliveries=int(total_deliveries),
        successful_deliveries=int(successful_deliveries),
        failed_deliveries=int(failed_deliveries),
        success_rate=round(success_rate, 2),
    )


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """Получить webhook по ID."""
    webhook = await get_webhook_by_id(db, webhook_id)
    if not webhook:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    return _serialize_webhook(webhook)


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook_endpoint(
    payload: WebhookCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """Создать новый webhook."""
    webhook = await create_webhook(
        db,
        name=payload.name,
        url=payload.url,
        event_type=payload.event_type,
        secret=payload.secret,
        description=payload.description,
    )
    return _serialize_webhook(webhook)


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook_endpoint(
    webhook_id: int,
    payload: WebhookUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """Обновить webhook."""
    webhook = await get_webhook_by_id(db, webhook_id)
    if not webhook:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")

    webhook = await update_webhook(
        db,
        webhook,
        name=payload.name,
        url=payload.url,
        secret=payload.secret,
        description=payload.description,
        is_active=payload.is_active,
    )
    return _serialize_webhook(webhook)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook_endpoint(
    webhook_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Удалить webhook."""
    webhook = await get_webhook_by_id(db, webhook_id)
    if not webhook:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")

    await delete_webhook(db, webhook)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{webhook_id}/deliveries", response_model=WebhookDeliveryListResponse)
async def list_webhook_deliveries(
    webhook_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = Query(default=None, alias="status"),
) -> WebhookDeliveryListResponse:
    """Список доставок webhook."""
    webhook = await get_webhook_by_id(db, webhook_id)
    if not webhook:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")

    query = select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id)

    if status_filter:
        query = query.where(WebhookDelivery.status == status_filter)

    # Подсчет общего количества
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query) or 0

    # Получение данных
    query = query.order_by(WebhookDelivery.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    deliveries = result.scalars().all()

    return WebhookDeliveryListResponse(
        items=[_serialize_delivery(delivery) for delivery in deliveries],
        total=int(total),
        limit=limit,
        offset=offset,
    )

