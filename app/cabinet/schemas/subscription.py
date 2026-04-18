"""Subscription schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel, Field


class ServerInfo(BaseModel):
    """Server info for display."""

    uuid: str
    name: str
    country_code: str | None = None


class TrafficPurchaseInfo(BaseModel):
    """Purchased traffic package info."""

    id: int
    traffic_gb: int
    expires_at: datetime
    created_at: datetime
    days_remaining: int
    progress_percent: float


class SubscriptionData(BaseModel):
    """User subscription data."""

    id: int
    name: str | None = None
    status: str
    is_trial: bool
    start_date: datetime
    end_date: datetime
    days_left: int
    hours_left: int = 0
    minutes_left: int = 0
    time_left_display: str = ''  # Human readable format like "2д 5ч" or "5ч 30м"
    traffic_limit_gb: int
    traffic_used_gb: float
    traffic_used_percent: float
    device_limit: int
    connected_squads: list[str] = []
    servers: list[ServerInfo] = []  # Server display info
    autopay_enabled: bool
    autopay_days_before: int
    subscription_url: str | None = None
    hide_subscription_link: bool = False  # Скрывать ли отображение ссылки (но кнопки работают)
    is_active: bool
    is_expired: bool
    is_limited: bool = False
    traffic_purchases: list[TrafficPurchaseInfo] = []
    # Daily tariff fields
    is_daily: bool = False
    is_daily_paused: bool = False
    daily_price_kopeks: int | None = None
    next_daily_charge_at: datetime | None = None  # When next daily charge will happen
    tariff_id: int | None = None
    tariff_name: str | None = None
    traffic_reset_mode: str | None = None

    class Config:
        from_attributes = True


# Backward compatibility alias
SubscriptionResponse = SubscriptionData


class SubscriptionStatusResponse(BaseModel):
    """Response for subscription status endpoint - handles users with and without subscription."""

    has_subscription: bool
    subscription: SubscriptionData | None = None


class RenewalOptionResponse(BaseModel):
    """Available subscription renewal option."""

    period_days: int
    price_kopeks: int
    price_rubles: float
    discount_percent: int = 0
    original_price_kopeks: int | None = None


class RenewalRequest(BaseModel):
    """Request to renew subscription."""

    period_days: int = Field(..., ge=1, le=3650, description='Renewal period in days')
    subscription_id: int | None = Field(
        default=None,
        description='ID of subscription to renew (required in multi-tariff mode)',
    )


class TrafficPackageResponse(BaseModel):
    """Available traffic package."""

    gb: int
    price_kopeks: int
    price_rubles: float
    is_unlimited: bool = False


class TrafficPurchaseRequest(BaseModel):
    """Request to purchase additional traffic."""

    gb: int = Field(..., ge=0, le=100_000, description='GB to purchase (0 = unlimited)')


class DevicePurchaseRequest(BaseModel):
    """Request to purchase additional device slots."""

    devices: int = Field(..., ge=1, le=100, description='Number of additional devices')


class AutopayUpdateRequest(BaseModel):
    """Request to update autopay settings."""

    enabled: bool
    days_before: int | None = Field(None, ge=1, le=30, description='Days before expiration to charge')


class TrialInfoResponse(BaseModel):
    """Trial subscription info."""

    is_available: bool
    duration_days: int
    traffic_limit_gb: int
    device_limit: int
    requires_payment: bool = False
    price_kopeks: int = 0
    price_rubles: float = 0.0
    reason_unavailable: str | None = None


# ============ Purchase Options Schemas ============


class PurchaseSelectionRequest(BaseModel):
    """User's selection for subscription purchase."""

    period_id: str | None = Field(None, description="Period ID like 'days:30'")
    period_days: int | None = Field(None, ge=1, le=3650, description='Period in days')
    traffic_value: int | None = Field(None, ge=0, le=100_000, description='Traffic in GB (0 = unlimited)')
    servers: list[str] | None = Field(default_factory=list, description='Server UUIDs')
    devices: int | None = Field(None, ge=1, le=100, description='Device limit')


class PurchasePreviewRequest(BaseModel):
    """Request to preview purchase pricing."""

    selection: PurchaseSelectionRequest
    name: str | None = Field(None, max_length=255, description='Optional custom subscription name')


# ============ Tariff Purchase Schemas ============


class TariffPurchaseRequest(BaseModel):
    """Request to purchase a tariff."""

    tariff_id: int = Field(..., description='Tariff ID to purchase')
    period_days: int = Field(..., ge=1, le=3650, description='Period in days')
    traffic_gb: int | None = Field(
        None, ge=0, le=100_000, description='Custom traffic in GB (for custom_traffic_enabled tariffs)'
    )
    name: str | None = Field(None, max_length=255, description='Optional custom subscription name')


class SubscriptionRenameRequest(BaseModel):
    """Request to rename a subscription."""

    name: str | None = Field(None, max_length=255, description='Custom subscription name (null to clear)')
