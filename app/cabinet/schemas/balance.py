"""Balance and payment schemas for cabinet."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BalanceResponse(BaseModel):
    """User balance data."""

    balance_kopeks: int
    balance_rubles: float


class TransactionResponse(BaseModel):
    """Transaction history item."""

    id: int
    type: str
    amount_kopeks: int
    amount_rubles: float
    description: str | None = None
    payment_method: str | None = None
    is_completed: bool
    created_at: datetime
    completed_at: datetime | None = None

    class Config:
        from_attributes = True


class TransactionListResponse(BaseModel):
    """Paginated transaction list."""

    items: list[TransactionResponse]
    total: int
    page: int
    per_page: int
    pages: int


class PaymentOptionResponse(BaseModel):
    """Payment method option (e.g. Platega sub-methods)."""

    id: str
    name: str
    description: str | None = None


class PaymentMethodResponse(BaseModel):
    """Available payment method."""

    id: str
    name: str
    description: str | None = None
    min_amount_kopeks: int
    max_amount_kopeks: int
    is_available: bool = True
    options: list[dict[str, Any]] | None = None


class TopUpRequest(BaseModel):
    """Request to create payment for balance top-up."""

    amount_kopeks: int = Field(..., ge=1000, description='Amount in kopeks (min 10 rubles)')
    payment_method: str = Field(..., description='Payment method ID')
    payment_option: str | None = Field(None, description='Payment option (e.g. Platega method code)')


class TopUpResponse(BaseModel):
    """Response with payment info."""

    payment_id: str
    payment_url: str
    amount_kopeks: int
    amount_rubles: float
    status: str
    expires_at: datetime | None = None


class StarsInvoiceRequest(BaseModel):
    """Request to create Telegram Stars invoice for balance top-up."""

    amount_kopeks: int = Field(..., ge=100, description='Amount in kopeks (min 1 ruble)')


class StarsInvoiceResponse(BaseModel):
    """Response with Telegram Stars invoice link."""

    invoice_url: str
    stars_amount: int
    amount_kopeks: int


class PendingPaymentResponse(BaseModel):
    """Pending payment details for manual verification."""

    id: int
    method: str
    method_display: str
    identifier: str
    amount_kopeks: int
    amount_rubles: float
    status: str
    status_emoji: str
    status_text: str
    is_paid: bool
    is_checkable: bool
    created_at: datetime
    expires_at: datetime | None = None
    payment_url: str | None = None
    user_id: int | None = None
    user_telegram_id: int | None = None
    user_username: str | None = None

    class Config:
        from_attributes = True


class PendingPaymentListResponse(BaseModel):
    """Paginated list of pending payments."""

    items: list[PendingPaymentResponse]
    total: int
    page: int
    per_page: int
    pages: int


class ManualCheckResponse(BaseModel):
    """Response after manual payment status check."""

    success: bool
    message: str
    payment: PendingPaymentResponse | None = None
    status_changed: bool = False
    old_status: str | None = None
    new_status: str | None = None
