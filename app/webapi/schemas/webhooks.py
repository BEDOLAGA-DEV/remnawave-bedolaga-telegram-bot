from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, HttpUrl


class WebhookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1, max_length=50)
    secret: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None)


class WebhookUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    url: Optional[str] = Field(default=None, min_length=1)
    secret: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class WebhookResponse(BaseModel):
    id: int
    name: str
    url: str
    event_type: str
    is_active: bool
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_triggered_at: Optional[datetime]
    failure_count: int
    success_count: int

    class Config:
        from_attributes = True


class WebhookListResponse(BaseModel):
    items: list[WebhookResponse]
    total: int
    limit: int
    offset: int


class WebhookDeliveryResponse(BaseModel):
    id: int
    webhook_id: int
    event_type: str
    payload: dict[str, Any]
    response_status: Optional[int]
    response_body: Optional[str]
    status: str
    error_message: Optional[str]
    attempt_number: int
    created_at: datetime
    delivered_at: Optional[datetime]
    next_retry_at: Optional[datetime]

    class Config:
        from_attributes = True


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse]
    total: int
    limit: int
    offset: int


class WebhookStatsResponse(BaseModel):
    total_webhooks: int
    active_webhooks: int
    total_deliveries: int
    successful_deliveries: int
    failed_deliveries: int
    success_rate: float

