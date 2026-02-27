"""Partner system schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel, Field


# ==================== User-facing ====================


class PartnerApplicationRequest(BaseModel):
    """Request to apply for partner status."""

    company_name: str | None = Field(None, max_length=255)
    website_url: str | None = Field(None, max_length=500)
    telegram_channel: str | None = Field(None, max_length=255)
    description: str | None = Field(None, max_length=2000)
    expected_monthly_referrals: int | None = Field(None, ge=0, le=2_000_000_000)
    desired_commission_percent: int | None = Field(None, ge=1, le=100)


class PartnerApplicationInfo(BaseModel):
    """Application info for the user."""

    id: int
    status: str
    company_name: str | None = None
    website_url: str | None = None
    telegram_channel: str | None = None
    description: str | None = None
    expected_monthly_referrals: int | None = None
    desired_commission_percent: int | None = None
    admin_comment: str | None = None
    approved_commission_percent: int | None = None
    created_at: datetime
    processed_at: datetime | None = None

    class Config:
        from_attributes = True


class PartnerCampaignInfo(BaseModel):
    """Campaign info visible to the partner."""

    id: int
    name: str
    start_parameter: str
    bonus_type: str
    balance_bonus_kopeks: int = 0
    subscription_duration_days: int | None = None
    subscription_traffic_gb: int | None = None
    deep_link: str | None = None
    web_link: str | None = None


class PartnerStatusResponse(BaseModel):
    """Partner status for current user."""

    partner_status: str
    commission_percent: int | None = None
    latest_application: PartnerApplicationInfo | None = None
    campaigns: list[PartnerCampaignInfo] = []


# ==================== Admin-facing ====================


class AdminPartnerApplicationItem(BaseModel):
    """Partner application in admin list."""

    id: int
    user_id: int
    username: str | None = None
    first_name: str | None = None
    telegram_id: int | None = None
    company_name: str | None = None
    website_url: str | None = None
    telegram_channel: str | None = None
    description: str | None = None
    expected_monthly_referrals: int | None = None
    desired_commission_percent: int | None = None
    status: str
    admin_comment: str | None = None
    approved_commission_percent: int | None = None
    created_at: datetime
    processed_at: datetime | None = None


class AdminPartnerApplicationsResponse(BaseModel):
    """List of partner applications."""

    items: list[AdminPartnerApplicationItem]
    total: int


class AdminApproveRequest(BaseModel):
    """Request to approve a partner application."""

    commission_percent: int = Field(..., ge=1, le=100)
    comment: str | None = Field(None, max_length=2000)


class AdminRejectRequest(BaseModel):
    """Request to reject a partner application."""

    comment: str | None = Field(None, max_length=2000)


class AdminPartnerItem(BaseModel):
    """Partner in admin list."""

    user_id: int
    username: str | None = None
    first_name: str | None = None
    telegram_id: int | None = None
    commission_percent: int | None = None
    total_referrals: int = 0
    total_earnings_kopeks: int = 0
    balance_kopeks: int = 0
    partner_status: str
    created_at: datetime


class AdminPartnerListResponse(BaseModel):
    """List of partners for admin."""

    items: list[AdminPartnerItem]
    total: int


class CampaignSummary(BaseModel):
    """Campaign summary for partner detail."""

    id: int
    name: str
    start_parameter: str
    is_active: bool


class AdminPartnerDetailResponse(BaseModel):
    """Detailed partner info for admin."""

    user_id: int
    username: str | None = None
    first_name: str | None = None
    telegram_id: int | None = None
    commission_percent: int | None = None
    partner_status: str
    balance_kopeks: int = 0
    total_referrals: int = 0
    paid_referrals: int = 0
    active_referrals: int = 0
    earnings_all_time: int = 0
    earnings_today: int = 0
    earnings_week: int = 0
    earnings_month: int = 0
    conversion_to_paid: float = 0.0
    campaigns: list[CampaignSummary] = []
    created_at: datetime


class AdminUpdateCommissionRequest(BaseModel):
    """Request to update partner commission."""

    commission_percent: int = Field(..., ge=1, le=100)
