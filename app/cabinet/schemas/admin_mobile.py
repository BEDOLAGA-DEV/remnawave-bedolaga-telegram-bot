"""Stable response schemas for the Bedolaga mobile cabinet v1 contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


CONTRACT_VERSION = 'bedolaga-mobile-cabinet-v1'
MOBILE_ALLOWED_ROLE_NAMES = ['Superadmin', 'Admin', 'Moderator']


class MobileContractMetadata(BaseModel):
    """Common contract metadata returned by mobile facade endpoints."""

    contract_version: str = CONTRACT_VERSION
    required_role_names: list[str] = Field(default_factory=lambda: MOBILE_ALLOWED_ROLE_NAMES.copy())
    auth: str = 'cabinet_jwt_allowed_role'
    legacy_auth: str = 'rejected'


class MobileContractInfoResponse(MobileContractMetadata):
    """Machine-readable contract summary."""

    realtime: str = 'disabled'
    media_download_auth: str = 'signed_media_token'
    refresh_validity_method: str = 'explicit-fields'
    refresh_fields: list[str] = Field(default_factory=lambda: ['expires_in', 'refresh_expires_in'])


class MobileSubscriptionResponse(MobileContractMetadata):
    """Subscription detail exposed to Wave Machine mobile clients."""

    id: int
    user_id: int
    status: str
    actual_status: str
    is_trial: bool
    start_date: datetime | None = None
    end_date: datetime | None = None
    traffic_limit_gb: int = 0
    traffic_used_gb: float = 0
    device_limit: int = 0
    autopay_enabled: bool = False
    autopay_days_before: int | None = None
    subscription_url: str | None = None
    subscription_crypto_link: str | None = None
    connected_squads: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MobileTransactionItem(BaseModel):
    """Transaction shape used by mobile spending and income screens."""

    id: int
    user_id: int
    type: str
    amount_kopeks: int
    amount_rubles: float
    description: str | None = None
    payment_method: str | None = None
    external_id: str | None = None
    is_completed: bool
    created_at: datetime
    completed_at: datetime | None = None


class MobileTransactionListResponse(MobileContractMetadata):
    """Paginated mobile transaction response."""

    items: list[MobileTransactionItem]
    total: int
    limit: int
    offset: int
    completed_real_payment_only: bool = True


class MobileIncomeResponse(MobileContractMetadata):
    """Income aggregate for a calendar month."""

    period_start: datetime
    period_end: datetime
    income_kopeks: int
    income_rubles: float
    transaction_count: int
    payment_methods: list[str]
    completed_real_payment_only: bool = True


class MobileDashboardStatsResponse(MobileContractMetadata):
    """Cabinet-JWT replacement for legacy /stats/full mobile usage."""

    overview: dict[str, Any]
    users: dict[str, Any]
    subscriptions: dict[str, Any]
    transactions: dict[str, Any]
    referrals: dict[str, Any]


class MobileDisabledFeatureResponse(MobileContractMetadata):
    """Explicit disabled-feature contract response."""

    feature: str
    enabled: bool = False
    replacement: str | None = None
    reason: str


class MobileSettingsCorsKey(BaseModel):
    """Server-side CORS setting key exposed after auth."""

    key: str
    mode: str
    env_locked: bool
    secret: bool = False


class MobileSettingsCorsContractResponse(MobileContractMetadata):
    """CORS contract for mobile setup/settings screens."""

    pre_auth_behavior: str
    server_side_edit: str
    allowed_keys: list[MobileSettingsCorsKey]
