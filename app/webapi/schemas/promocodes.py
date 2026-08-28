from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.database.models import PromoCodeType


class PromoCodeResponse(BaseModel):
    id: int
    code: str
    type: PromoCodeType
    balance_bonus_kopeks: int
    balance_bonus_rubles: float
    subscription_days: int
    traffic_gb: int = 0
    max_uses: int
    current_uses: int
    uses_left: int
    is_active: bool
    is_valid: bool
    valid_from: datetime
    valid_until: datetime | None = None
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class PromoCodeListResponse(BaseModel):
    items: list[PromoCodeResponse]
    total: int
    limit: int
    offset: int


class PromoCodeCreateRequest(BaseModel):
    code: str
    type: PromoCodeType
    balance_bonus_kopeks: int = 0
    subscription_days: int = 0
    traffic_gb: int = 0
    max_uses: int = Field(default=1, ge=0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool = True
    created_by: int | None = None


class PromoCodeUpdateRequest(BaseModel):
    code: str | None = None
    type: PromoCodeType | None = None
    balance_bonus_kopeks: int | None = None
    subscription_days: int | None = None
    traffic_gb: int | None = None
    max_uses: int | None = Field(default=None, ge=0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool | None = None


class PromoCodeRecentUse(BaseModel):
    id: int
    user_id: int
    user_username: str | None = None
    user_full_name: str | None = None
    user_telegram_id: int | None = None
    used_at: datetime


class PromoCodeDetailResponse(PromoCodeResponse):
    total_uses: int
    today_uses: int
    recent_uses: list[PromoCodeRecentUse] = Field(default_factory=list)
