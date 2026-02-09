"""Schemas for Admin Users management in cabinet."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class UserStatusEnum(str, Enum):
    """User status enum."""

    ACTIVE = 'active'
    BLOCKED = 'blocked'
    DELETED = 'deleted'


class SubscriptionStatusEnum(str, Enum):
    """Subscription status enum."""

    TRIAL = 'trial'
    ACTIVE = 'active'
    EXPIRED = 'expired'
    DISABLED = 'disabled'
    PENDING = 'pending'


class SortByEnum(str, Enum):
    """Sort options for users list."""

    CREATED_AT = 'created_at'
    BALANCE = 'balance'
    TRAFFIC = 'traffic'
    LAST_ACTIVITY = 'last_activity'
    TOTAL_SPENT = 'total_spent'
    PURCHASE_COUNT = 'purchase_count'


# === User Subscription Info ===


class TrafficPurchaseItem(BaseModel):
    """Individual traffic purchase record."""

    id: int
    traffic_gb: int
    expires_at: datetime
    created_at: datetime
    days_remaining: int
    is_expired: bool


class UserSubscriptionInfo(BaseModel):
    """User subscription information."""

    id: int
    status: str
    is_trial: bool
    start_date: datetime | None = None
    end_date: datetime | None = None
    traffic_limit_gb: int = 0
    traffic_used_gb: float = 0.0
    device_limit: int = 1
    tariff_id: int | None = None
    tariff_name: str | None = None
    autopay_enabled: bool = False
    is_active: bool = False
    days_remaining: int = 0
    purchased_traffic_gb: int = 0
    traffic_purchases: list[TrafficPurchaseItem] = []


class UserPromoGroupInfo(BaseModel):
    """User promo group info."""

    id: int
    name: str
    is_default: bool = False


# === User List ===


class UserListItem(BaseModel):
    """User item in list."""

    id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    status: str
    balance_kopeks: int
    balance_rubles: float
    created_at: datetime
    last_activity: datetime | None = None

    # Subscription summary
    has_subscription: bool = False
    subscription_status: str | None = None
    subscription_is_trial: bool = False
    subscription_end_date: datetime | None = None

    # Promo group
    promo_group_id: int | None = None
    promo_group_name: str | None = None

    # Stats
    total_spent_kopeks: int = 0
    purchase_count: int = 0

    # Restrictions
    has_restrictions: bool = False
    restriction_topup: bool = False
    restriction_subscription: bool = False


class UsersListResponse(BaseModel):
    """Paginated list of users."""

    users: list[UserListItem]
    total: int
    offset: int = 0
    limit: int = 50


# === User Detail ===


class UserTransactionItem(BaseModel):
    """User transaction."""

    id: int
    type: str
    amount_kopeks: int
    amount_rubles: float
    description: str | None = None
    payment_method: str | None = None
    is_completed: bool = True
    created_at: datetime


class UserReferralInfo(BaseModel):
    """User referral info."""

    referral_code: str
    referrals_count: int = 0
    total_earnings_kopeks: int = 0
    commission_percent: int | None = None
    referred_by_id: int | None = None
    referred_by_username: str | None = None


class UserDetailResponse(BaseModel):
    """Detailed user information."""

    id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str
    status: str
    language: str
    balance_kopeks: int
    balance_rubles: float

    # Email (cabinet)
    email: str | None = None
    email_verified: bool = False

    # Dates
    created_at: datetime
    updated_at: datetime | None = None
    last_activity: datetime | None = None
    cabinet_last_login: datetime | None = None

    # Subscription
    subscription: UserSubscriptionInfo | None = None

    # Promo group
    promo_group: UserPromoGroupInfo | None = None

    # Referral
    referral: UserReferralInfo

    # Stats
    total_spent_kopeks: int = 0
    purchase_count: int = 0
    used_promocodes: int = 0
    has_had_paid_subscription: bool = False
    lifetime_used_traffic_bytes: int = 0

    # Restrictions
    restriction_topup: bool = False
    restriction_subscription: bool = False
    restriction_reason: str | None = None

    # Promo offer
    promo_offer_discount_percent: int = 0
    promo_offer_discount_source: str | None = None
    promo_offer_discount_expires_at: datetime | None = None

    # Campaign
    campaign_name: str | None = None
    campaign_id: int | None = None

    # Recent transactions
    recent_transactions: list[UserTransactionItem] = []

    # Remnawave UUID
    remnawave_uuid: str | None = None


# === Panel Info ===


class UserPanelInfoResponse(BaseModel):
    """Panel info for user from Remnawave."""

    found: bool = False
    trojan_password: str | None = None
    vless_uuid: str | None = None
    ss_password: str | None = None
    subscription_url: str | None = None
    happ_link: str | None = None
    used_traffic_bytes: int = 0
    lifetime_used_traffic_bytes: int = 0
    traffic_limit_bytes: int = 0
    first_connected_at: datetime | None = None
    online_at: datetime | None = None
    last_connected_node_uuid: str | None = None
    last_connected_node_name: str | None = None


# === Node Usage ===


class UserNodeUsageItem(BaseModel):
    """Per-node traffic usage item."""

    node_uuid: str
    node_name: str
    country_code: str = ''
    total_bytes: int
    daily_bytes: list[int] = []


class UserNodeUsageResponse(BaseModel):
    """Node usage response with 30-day daily breakdown."""

    items: list[UserNodeUsageItem]
    categories: list[str] = []
    period_days: int = 30


# === User Actions ===


class UpdateBalanceRequest(BaseModel):
    """Request to update user balance."""

    amount_kopeks: int = Field(..., description='Amount in kopeks (positive to add, negative to subtract)')
    description: str = Field(default='Admin balance adjustment', max_length=500)
    create_transaction: bool = Field(default=True, description='Create transaction record')


class UpdateBalanceResponse(BaseModel):
    """Response after balance update."""

    success: bool
    old_balance_kopeks: int
    new_balance_kopeks: int
    message: str


class UpdateSubscriptionRequest(BaseModel):
    """Request to update user subscription."""

    action: str = Field(
        ..., description='Action: extend, set_end_date, change_tariff, set_traffic, toggle_autopay, cancel'
    )

    # For extend action
    days: int | None = Field(None, ge=1, le=3650, description='Days to extend')

    # For set_end_date action
    end_date: datetime | None = Field(None, description='New end date')

    # For change_tariff action
    tariff_id: int | None = Field(None, description='New tariff ID')

    # For set_traffic action
    traffic_limit_gb: int | None = Field(None, ge=0, description='New traffic limit in GB')
    traffic_used_gb: float | None = Field(None, ge=0, description='Set traffic used in GB')

    # For toggle_autopay
    autopay_enabled: bool | None = Field(None, description='Enable/disable autopay')

    # For add_traffic action
    traffic_gb: int | None = Field(None, ge=1, description='Traffic GB to add')

    # For remove_traffic action
    traffic_purchase_id: int | None = Field(None, description='Traffic purchase ID to remove')

    # For create new subscription
    is_trial: bool | None = Field(None, description='Is trial subscription')
    device_limit: int | None = Field(None, ge=1, description='Device limit')


class UpdateSubscriptionResponse(BaseModel):
    """Response after subscription update."""

    success: bool
    message: str
    subscription: UserSubscriptionInfo | None = None


class UpdateUserStatusRequest(BaseModel):
    """Request to update user status."""

    status: UserStatusEnum
    reason: str | None = Field(None, max_length=500, description='Reason for status change')


class UpdateUserStatusResponse(BaseModel):
    """Response after status update."""

    success: bool
    old_status: str
    new_status: str
    message: str


class UpdateRestrictionsRequest(BaseModel):
    """Request to update user restrictions."""

    restriction_topup: bool | None = Field(None, description='Block balance top-up')
    restriction_subscription: bool | None = Field(None, description='Block subscription purchase/renewal')
    restriction_reason: str | None = Field(None, max_length=500, description='Reason for restrictions')


class UpdateRestrictionsResponse(BaseModel):
    """Response after restrictions update."""

    success: bool
    restriction_topup: bool
    restriction_subscription: bool
    restriction_reason: str | None = None
    message: str


class UpdatePromoGroupRequest(BaseModel):
    """Request to update user promo group."""

    promo_group_id: int | None = Field(None, description='New promo group ID (null to remove)')


class UpdatePromoGroupResponse(BaseModel):
    """Response after promo group update."""

    success: bool
    old_promo_group_id: int | None = None
    new_promo_group_id: int | None = None
    promo_group_name: str | None = None
    message: str


class UpdateReferralCommissionRequest(BaseModel):
    """Request to update user referral commission percent."""

    commission_percent: int | None = Field(
        None, ge=0, le=100, description='Referral commission percent (null for default)'
    )


class UpdateReferralCommissionResponse(BaseModel):
    """Response after referral commission update."""

    success: bool
    old_commission_percent: int | None = None
    new_commission_percent: int | None = None
    message: str


class DeviceInfo(BaseModel):
    """Individual device info."""

    hwid: str
    platform: str = ''
    device_model: str = ''
    created_at: str | None = None


class UserDevicesResponse(BaseModel):
    """User devices from panel."""

    devices: list[DeviceInfo] = []
    total: int = 0
    device_limit: int = 0


class DeleteDeviceResponse(BaseModel):
    """Response after device deletion."""

    success: bool
    message: str
    deleted_hwid: str | None = None


class ResetDevicesResponse(BaseModel):
    """Response after resetting all devices."""

    success: bool
    message: str
    deleted_count: int = 0


class DeleteUserRequest(BaseModel):
    """Request to delete user."""

    soft_delete: bool = Field(default=True, description='Soft delete (mark as deleted) or hard delete')
    reason: str | None = Field(None, max_length=500, description='Reason for deletion')


class DeleteUserResponse(BaseModel):
    """Response after user deletion."""

    success: bool
    message: str


# === Statistics ===


class UsersStatsResponse(BaseModel):
    """Users statistics."""

    total_users: int = 0
    active_users: int = 0
    blocked_users: int = 0
    deleted_users: int = 0
    new_today: int = 0
    new_week: int = 0
    new_month: int = 0

    # Subscription stats
    users_with_subscription: int = 0
    users_with_active_subscription: int = 0
    users_with_trial: int = 0
    users_with_expired_subscription: int = 0

    # Financial stats
    total_balance_kopeks: int = 0
    total_balance_rubles: float = 0.0
    avg_balance_kopeks: int = 0

    # Activity stats
    active_today: int = 0
    active_week: int = 0
    active_month: int = 0


# === Search ===


class UserSearchRequest(BaseModel):
    """Request for user search."""

    query: str = Field(..., min_length=1, max_length=255)
    search_by: list[str] = Field(
        default=['telegram_id', 'username', 'first_name', 'last_name', 'email'], description='Fields to search in'
    )
    limit: int = Field(default=20, ge=1, le=100)


# === Tariffs for User ===


class PeriodPriceInfo(BaseModel):
    """Period price info."""

    days: int
    price_kopeks: int
    price_rubles: float


class UserAvailableTariffItem(BaseModel):
    """Tariff available for user."""

    id: int
    name: str
    description: str | None = None
    is_active: bool = True
    is_trial_available: bool = False
    traffic_limit_gb: int = 0
    device_limit: int = 1
    tier_level: int = 1
    display_order: int = 0

    # Pricing
    period_prices: list[PeriodPriceInfo] = []
    is_daily: bool = False
    daily_price_kopeks: int = 0

    # Custom options
    custom_days_enabled: bool = False
    price_per_day_kopeks: int = 0
    min_days: int = 1
    max_days: int = 365

    # Device limits
    device_price_kopeks: int | None = None
    max_device_limit: int | None = None

    # Traffic topup
    traffic_topup_enabled: bool = False
    traffic_topup_packages: dict[str, int] = {}
    max_topup_traffic_gb: int = 0

    # Access info
    is_available: bool = True  # Available for this user's promo group
    requires_promo_group: bool = False  # Requires specific promo group


class UserAvailableTariffsResponse(BaseModel):
    """List of tariffs available for user."""

    user_id: int
    promo_group_id: int | None = None
    promo_group_name: str | None = None
    tariffs: list[UserAvailableTariffItem] = []
    total: int = 0

    # Current subscription tariff
    current_tariff_id: int | None = None
    current_tariff_name: str | None = None


# === Panel Sync ===


class PanelUserInfo(BaseModel):
    """User info from panel."""

    uuid: str | None = None
    short_uuid: str | None = None
    username: str | None = None
    status: str | None = None
    expire_at: datetime | None = None
    traffic_limit_gb: float = 0
    traffic_used_gb: float = 0
    device_limit: int = 1
    subscription_url: str | None = None
    active_squads: list[str] = []


class SyncFromPanelRequest(BaseModel):
    """Request to sync user from panel."""

    update_subscription: bool = Field(default=True, description='Update subscription data')
    update_traffic: bool = Field(default=True, description='Update traffic usage')
    create_if_missing: bool = Field(
        default=False, description='Create subscription if user exists in panel but not in bot'
    )


class SyncFromPanelResponse(BaseModel):
    """Response after syncing from panel."""

    success: bool
    message: str
    panel_user: PanelUserInfo | None = None
    changes: dict[str, Any] = {}
    errors: list[str] = []


class SyncToPanelRequest(BaseModel):
    """Request to sync user to panel."""

    create_if_missing: bool = Field(default=True, description='Create user in panel if not exists')
    update_status: bool = Field(default=True, description='Update user status in panel')
    update_traffic_limit: bool = Field(default=True, description='Update traffic limit in panel')
    update_expire_date: bool = Field(default=True, description='Update expire date in panel')
    update_squads: bool = Field(default=True, description='Update connected squads in panel')


class SyncToPanelResponse(BaseModel):
    """Response after syncing to panel."""

    success: bool
    message: str
    action: str = ''  # created, updated, no_changes
    panel_uuid: str | None = None
    changes: dict[str, Any] = {}
    errors: list[str] = []


class PanelSyncStatusResponse(BaseModel):
    """Panel sync status for user."""

    user_id: int
    telegram_id: int | None = None
    remnawave_uuid: str | None = None
    last_sync: datetime | None = None

    # Bot data
    bot_subscription_status: str | None = None
    bot_subscription_end_date: datetime | None = None
    bot_traffic_limit_gb: int = 0
    bot_traffic_used_gb: float = 0
    bot_device_limit: int = 0
    bot_squads: list[str] = []

    # Panel data (if available)
    panel_found: bool = False
    panel_status: str | None = None
    panel_expire_at: datetime | None = None
    panel_traffic_limit_gb: float = 0
    panel_traffic_used_gb: float = 0
    panel_device_limit: int = 0
    panel_squads: list[str] = []

    # Differences
    has_differences: bool = False
    differences: list[str] = []


# === Admin User Management Actions ===


class FullDeleteUserRequest(BaseModel):
    """Request for full user deletion (bot + panel)."""

    delete_from_panel: bool = Field(default=True, description='Also delete user from Remnawave panel')
    reason: str | None = Field(None, max_length=500, description='Reason for deletion')


class FullDeleteUserResponse(BaseModel):
    """Response after full user deletion."""

    success: bool
    message: str
    deleted_from_bot: bool = False
    deleted_from_panel: bool = False
    panel_error: str | None = None


class ResetTrialRequest(BaseModel):
    """Request to reset user trial."""

    reason: str | None = Field(None, max_length=500, description='Reason for trial reset')


class ResetTrialResponse(BaseModel):
    """Response after trial reset."""

    success: bool
    message: str
    subscription_deleted: bool = False
    has_used_trial_reset: bool = False


class ResetSubscriptionRequest(BaseModel):
    """Request to reset user subscription."""

    deactivate_in_panel: bool = Field(default=True, description='Also deactivate in Remnawave panel')
    reason: str | None = Field(None, max_length=500, description='Reason for subscription reset')


class ResetSubscriptionResponse(BaseModel):
    """Response after subscription reset."""

    success: bool
    message: str
    subscription_deleted: bool = False
    panel_deactivated: bool = False
    panel_error: str | None = None


class DisableUserRequest(BaseModel):
    """Request to disable user."""

    reason: str | None = Field(None, max_length=500, description='Reason for disabling')


class DisableUserResponse(BaseModel):
    """Response after user disable."""

    success: bool
    message: str
    subscription_deactivated: bool = False
    panel_deactivated: bool = False
    user_blocked: bool = False
    panel_error: str | None = None
