from __future__ import annotations

from datetime import datetime

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
    autopay_days_before: int | None = None
    subscription_url: str | None = None
    subscription_crypto_link: str | None = None
    connected_squads: list[str] = Field(default_factory=list)
    tariff_id: int | None = None
    tariff_name: str | None = None


class UserResponse(BaseModel):
    id: int
    telegram_id: int | None = None
    email: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    status: str
    language: str
    balance_kopeks: int
    balance_rubles: float
    referral_code: str | None = None
    referred_by_id: int | None = None
    has_had_paid_subscription: bool
    has_made_first_topup: bool
    created_at: datetime
    updated_at: datetime
    last_activity: datetime | None = None
    promo_group: PromoGroupSummary | None = None
    subscription: SubscriptionSummary | None = None
    subscriptions: list[SubscriptionSummary] = Field(default_factory=list)


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    limit: int
    offset: int


class UserCreateRequest(BaseModel):
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language: str = 'ru'
    referred_by_id: int | None = None
    promo_group_id: int | None = None


class UserUpdateRequest(BaseModel):
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language: str | None = None
    status: str | None = None
    promo_group_id: int | None = None
    referral_code: str | None = None
    has_had_paid_subscription: bool | None = None
    has_made_first_topup: bool | None = None


class BalanceUpdateRequest(BaseModel):
    amount_kopeks: int = Field(..., ge=-100_000_000, le=100_000_000)
    description: str | None = Field(default='Корректировка через веб-API')
    create_transaction: bool = True


class UserSubscriptionCreateRequest(BaseModel):
    """Схема для создания подписки через users API (user_id берется из URL)"""

    is_trial: bool = False
    duration_days: int | None = None
    traffic_limit_gb: int | None = None
    device_limit: int | None = None
    squad_uuid: str | None = None
    connected_squads: list[str] | None = None
    replace_existing: bool = False
    subscription_id: int | None = Field(
        default=None,
        description='ID of existing subscription to replace (required in multi-tariff mode when replace_existing=true)',
    )
