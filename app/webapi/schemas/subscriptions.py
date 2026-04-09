from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    status: str
    actual_status: str
    is_trial: bool
    start_date: datetime
    end_date: datetime
    traffic_limit_gb: int
    traffic_used_gb: float
    device_limit: int
    autopay_enabled: bool
    autopay_days_before: int | None = None
    subscription_url: str | None = None
    subscription_crypto_link: str | None = None
    connected_squads: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SubscriptionCreateRequest(BaseModel):
    user_id: int
    is_trial: bool = False
    duration_days: int | None = Field(None, ge=1, le=36500)
    traffic_limit_gb: int | None = Field(None, ge=0, le=1_000_000)
    device_limit: int | None = Field(None, ge=1, le=10_000)
    squad_uuid: str | None = None
    connected_squads: list[str] | None = None
    replace_existing: bool = False
    subscription_id: int | None = Field(
        default=None,
        description='ID of existing subscription to replace (required in multi-tariff mode when replace_existing=true)',
    )


class SubscriptionExtendRequest(BaseModel):
    days: int = Field(..., gt=0, le=36500)


class SubscriptionTrafficRequest(BaseModel):
    gb: int = Field(..., gt=0, le=1_000_000)


class SubscriptionDevicesRequest(BaseModel):
    devices: int = Field(..., gt=0, le=10_000)


class SubscriptionSquadRequest(BaseModel):
    squad_uuid: str
