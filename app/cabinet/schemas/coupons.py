"""Schemas for wholesale coupon batches management in cabinet."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CouponBatchResponse(BaseModel):
    """Coupon batch with redemption stats."""

    id: int
    name: str
    tariff_id: int | None
    tariff_name: str | None
    period_days: int
    coupons_total: int
    wholesale_price_kopeks: int
    valid_until: datetime | None
    is_revoked: bool
    created_at: datetime
    active_count: int = 0
    redeemed_count: int = 0
    revoked_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class CouponBatchListResponse(BaseModel):
    items: list[CouponBatchResponse]
    total: int
    limit: int
    offset: int


class CouponBatchCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description='Batch label, e.g. the partner name')
    tariff_id: int = Field(..., ge=1)
    period_days: int = Field(..., ge=1, le=3650)
    coupons_count: int = Field(..., ge=1, le=500)
    wholesale_price_kopeks: int = Field(0, ge=0, description='Bookkeeping-only price per coupon')
    valid_days: int = Field(0, ge=0, le=3650, description='Coupon lifetime in days; 0 — perpetual')


class CouponBatchCreatedResponse(CouponBatchResponse):
    """Creation response: batch card plus the generated one-time links."""

    links: list[str] = Field(default_factory=list)
    tokens: list[str] = Field(default_factory=list)


class CouponBatchLinksResponse(BaseModel):
    """Export of still-active coupon links of a batch."""

    batch_id: int
    count: int
    links: list[str] = Field(default_factory=list)
    tokens: list[str] = Field(default_factory=list)


class CouponBatchRevokeResponse(BaseModel):
    revoked_count: int
    batch: CouponBatchResponse


class CouponRedeemRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=64, description='Coupon token from the one-time link')


class CouponRedeemResponse(BaseModel):
    success: bool = True
    tariff_name: str
    period_days: int
    renewed: bool
    end_date: datetime | None


class CouponStatusResponse(BaseModel):
    """Public info about a still-redeemable coupon."""

    tariff_name: str
    period_days: int
    valid_until: datetime | None
    bot_link: str | None
