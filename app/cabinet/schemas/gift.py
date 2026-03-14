"""Schemas for cabinet gift subscription feature."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class GiftConfigSubOption(BaseModel):
    id: str
    name: str


class GiftConfigTariffPeriod(BaseModel):
    days: int
    price_kopeks: int
    price_label: str
    original_price_kopeks: int | None = None
    discount_percent: int | None = None


class GiftConfigTariff(BaseModel):
    id: int
    name: str
    description: str | None = None
    traffic_limit_gb: int
    device_limit: int
    periods: list[GiftConfigTariffPeriod]


class GiftConfigPaymentMethod(BaseModel):
    method_id: str
    display_name: str
    description: str | None = None
    icon_url: str | None = None
    min_amount_kopeks: int | None = None
    max_amount_kopeks: int | None = None
    sub_options: list[GiftConfigSubOption] | None = None


class GiftConfigResponse(BaseModel):
    is_enabled: bool
    tariffs: list[GiftConfigTariff] = []
    payment_methods: list[GiftConfigPaymentMethod] = []
    balance_kopeks: int = 0
    currency_symbol: str = '\u20bd'
    promo_group_name: str | None = None
    active_discount_percent: int | None = None
    active_discount_expires_at: datetime | None = None


class GiftPurchaseRequest(BaseModel):
    tariff_id: int = Field(gt=0)
    period_days: int = Field(gt=0, le=3650)
    recipient_type: str | None = Field(default=None, pattern=r'^(email|telegram)$')
    recipient_value: str | None = Field(default=None, max_length=255)
    gift_message: str | None = Field(default=None, max_length=1000)
    payment_mode: str = Field(pattern=r'^(balance|gateway)$')
    payment_method: str | None = Field(default=None, max_length=50)

    @model_validator(mode='after')
    def validate_payment(self) -> GiftPurchaseRequest:
        if self.payment_mode == 'gateway' and not self.payment_method:
            raise ValueError('payment_method is required for gateway mode')
        return self


class GiftPurchaseResponse(BaseModel):
    status: str
    purchase_token: str
    payment_url: str | None = None
    warning: str | None = None


class GiftPurchaseStatusResponse(BaseModel):
    status: str
    is_gift: bool = True
    is_code_only: bool = False
    purchase_token: str | None = None
    recipient_contact_value: str | None = None
    gift_message: str | None = None
    tariff_name: str | None = None
    period_days: int | None = None
    warning: str | None = None


class PendingGiftResponse(BaseModel):
    token: str
    tariff_name: str | None = None
    period_days: int
    gift_message: str | None = None
    sender_display: str | None = None
    created_at: datetime | None = None


class SentGiftResponse(BaseModel):
    """A gift the current user has sent."""

    token: str
    tariff_name: str | None = None
    period_days: int
    device_limit: int = 1
    status: str
    gift_recipient_value: str | None = None
    gift_message: str | None = None
    activated_by_username: str | None = None
    created_at: datetime | None = None


class ReceivedGiftResponse(BaseModel):
    """A gift the current user has received."""

    token: str
    tariff_name: str | None = None
    period_days: int
    device_limit: int = 1
    status: str
    sender_display: str | None = None
    gift_message: str | None = None
    created_at: datetime | None = None


class ActivateGiftRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100)


class ActivateGiftResponse(BaseModel):
    status: str
    tariff_name: str | None = None
    period_days: int | None = None
