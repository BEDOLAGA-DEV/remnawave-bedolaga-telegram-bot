"""Referral program schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel


class ReferralInfoResponse(BaseModel):
    """Referral program info for current user."""

    referral_code: str
    referral_link: str
    total_referrals: int
    active_referrals: int
    total_earnings_kopeks: int
    total_earnings_rubles: float
    commission_percent: int
    available_balance_kopeks: int = 0
    available_balance_rubles: float = 0
    withdrawn_kopeks: int = 0


class ReferralItemResponse(BaseModel):
    """Single referral info."""

    id: int
    username: str | None = None
    first_name: str | None = None
    created_at: datetime
    has_subscription: bool
    has_paid: bool


class ReferralListResponse(BaseModel):
    """Paginated referral list."""

    items: list[ReferralItemResponse]
    total: int
    page: int
    per_page: int
    pages: int


class ReferralEarningResponse(BaseModel):
    """Referral earning history item."""

    id: int
    amount_kopeks: int
    amount_rubles: float
    reason: str
    referral_username: str | None = None
    referral_first_name: str | None = None
    campaign_name: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class ReferralEarningsListResponse(BaseModel):
    """Paginated referral earnings list."""

    items: list[ReferralEarningResponse]
    total: int
    total_amount_kopeks: int
    total_amount_rubles: float
    page: int
    per_page: int
    pages: int


class ReferralTermsResponse(BaseModel):
    """Referral program terms."""

    is_enabled: bool
    commission_percent: int
    minimum_topup_kopeks: int
    minimum_topup_rubles: float
    first_topup_bonus_kopeks: int
    first_topup_bonus_rubles: float
    inviter_bonus_kopeks: int
    inviter_bonus_rubles: float
    partner_section_visible: bool = True
