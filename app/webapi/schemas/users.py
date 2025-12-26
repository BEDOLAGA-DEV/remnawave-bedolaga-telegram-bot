from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class PromoGroupSummary(BaseModel):
    id: int
    name: str
    server_discount_percent: int
    traffic_discount_percent: int
    device_discount_percent: int
    apply_discounts_to_addons: bool = True


class SubscriptionSummary(BaseModel):
    id: int
    status: str
    actual_status: str
    is_trial: bool
    start_date: datetime
    end_date: datetime
    traffic_limit_gb: int
    traffic_used_gb: float
    device_limit: int
    autopay_enabled: bool
    autopay_days_before: int
    subscription_url: Optional[str] = None
    subscription_crypto_link: Optional[str] = None
    connected_squads: List[str] = Field(default_factory=list)


class UserResponse(BaseModel):
    id: int
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: str
    language: str
    balance_kopeks: int
    balance_rubles: float
    referral_code: Optional[str] = None
    referred_by_id: Optional[int] = None
    has_had_paid_subscription: bool
    has_made_first_topup: bool
    created_at: datetime
    updated_at: datetime
    last_activity: Optional[datetime] = None
    promo_group: Optional[PromoGroupSummary] = None
    subscription: Optional[SubscriptionSummary] = None


class UserListResponse(BaseModel):
    items: List[UserResponse]
    total: int
    limit: int
    offset: int


class UserCreateRequest(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language: str = "ru"
    referred_by_id: Optional[int] = None
    promo_group_id: Optional[int] = None


class UserUpdateRequest(BaseModel):
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language: Optional[str] = None
    status: Optional[str] = None
    promo_group_id: Optional[int] = None
    referral_code: Optional[str] = None
    has_had_paid_subscription: Optional[bool] = None
    has_made_first_topup: Optional[bool] = None


class BalanceUpdateRequest(BaseModel):
    amount_kopeks: int
    description: Optional[str] = Field(default="Корректировка через веб-API")
    create_transaction: bool = True


class UserSubscriptionCreateRequest(BaseModel):
    """Схема для создания подписки через users API (user_id берется из URL)"""
    is_trial: bool = False
    duration_days: Optional[int] = None
    traffic_limit_gb: Optional[int] = None
    device_limit: Optional[int] = None
    squad_uuid: Optional[str] = None
    connected_squads: Optional[List[str]] = None
    replace_existing: bool = False