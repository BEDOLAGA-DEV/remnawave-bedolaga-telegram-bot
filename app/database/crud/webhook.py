from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Webhook, WebhookDelivery


async def create_webhook(
    db: AsyncSession,
    name: str,
    url: str,
    event_type: str,
    secret: Optional[str] = None,
    description: Optional[str] = None,
) -> Webhook:
    """Создать новый webhook."""
    webhook = Webhook(
        name=name,
        url=url,
        event_type=event_type,
        secret=secret,
        description=description,
        is_active=True,
    )
    db.add(webhook)
    await db.commit()
    await db.refresh(webhook)
    return webhook


async def get_webhook_by_id(db: AsyncSession, webhook_id: int) -> Optional[Webhook]:
    """Получить webhook по ID."""
    result = await db.execute(select(Webhook).where(Webhook.id == webhook_id))
    return result.scalar_one_or_none()


async def list_webhooks(
    db: AsyncSession,
    event_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Webhook], int]:
    """Список webhooks с фильтрами."""
    query = select(Webhook)

    if event_type:
        query = query.where(Webhook.event_type == event_type)
    if is_active is not None:
        query = query.where(Webhook.is_active == is_active)

    # Подсчет общего количества
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query) or 0

    # Получение данных
    query = query.order_by(Webhook.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    webhooks = result.scalars().all()

    return list(webhooks), int(total)


async def get_active_webhooks_for_event(
    db: AsyncSession,
    event_type: str,
) -> list[Webhook]:
    """Получить все активные webhooks для конкретного события."""
    result = await db.execute(
        select(Webhook)
        .where(Webhook.event_type == event_type)
        .where(Webhook.is_active == True)
    )
    return list(result.scalars().all())


async def update_webhook(
    db: AsyncSession,
    webhook: Webhook,
    name: Optional[str] = None,
    url: Optional[str] = None,
    secret: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Webhook:
    """Обновить webhook."""
    if name is not None:
        webhook.name = name
    if url is not None:
        webhook.url = url
    if secret is not None:
        webhook.secret = secret
    if description is not None:
        webhook.description = description
    if is_active is not None:
        webhook.is_active = is_active

    webhook.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(webhook)
    return webhook


async def delete_webhook(db: AsyncSession, webhook: Webhook) -> None:
    """Удалить webhook."""
    await db.delete(webhook)
    await db.commit()


async def record_webhook_delivery(
    db: AsyncSession,
    webhook_id: int,
    event_type: str,
    payload: dict,
    status: str,
    response_status: Optional[int] = None,
    response_body: Optional[str] = None,
    error_message: Optional[str] = None,
    attempt_number: int = 1,
) -> WebhookDelivery:
    """Записать попытку доставки webhook."""
    delivery = WebhookDelivery(
        webhook_id=webhook_id,
        event_type=event_type,
        payload=payload,
        status=status,
        response_status=response_status,
        response_body=response_body,
        error_message=error_message,
        attempt_number=attempt_number,
        delivered_at=datetime.utcnow() if status == "success" else None,
    )
    db.add(delivery)
    await db.commit()
    await db.refresh(delivery)
    return delivery


async def update_webhook_stats(
    db: AsyncSession,
    webhook: Webhook,
    success: bool,
) -> Webhook:
    """Обновить статистику webhook."""
    if success:
        webhook.success_count += 1
    else:
        webhook.failure_count += 1
    webhook.last_triggered_at = datetime.utcnow()
    await db.commit()
    await db.refresh(webhook)
    return webhook

