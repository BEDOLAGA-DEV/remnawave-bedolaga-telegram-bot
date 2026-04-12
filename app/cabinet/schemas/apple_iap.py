"""Apple In-App Purchase schemas for cabinet."""

from pydantic import BaseModel, Field


class ApplePurchaseRequest(BaseModel):
    """Request to verify and credit an Apple IAP transaction."""

    product_id: str = Field(..., description='Apple product ID (e.g. com.bitnet.vpnclient.topup.100)')
    transaction_id: str = Field(..., description='Apple StoreKit transaction ID')
    environment: str = Field('Production', description='Sandbox or Production')


class ApplePurchaseResponse(BaseModel):
    """Response indicating whether the purchase was successfully credited."""

    success: bool
