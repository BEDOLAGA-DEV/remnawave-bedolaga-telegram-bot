"""Schemas for partner promo admin endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PartnerPromoResponse(BaseModel):
    """Full partner promo response."""

    id: int
    title: dict
    description: dict
    url: str
    image_url: str | None = None
    is_active: bool
    sort_order: int
    click_count: int
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PartnerPromoCreateRequest(BaseModel):
    """Request to create a partner promo."""

    title: dict = Field(default_factory=dict)
    url: str = Field(min_length=1, max_length=2048)
    description: dict = Field(default_factory=dict)
    image_url: str | None = Field(None, max_length=2048)
    is_active: bool = True
    sort_order: int = 0


class PartnerPromoUpdateRequest(BaseModel):
    """Request to update a partner promo (all fields optional)."""

    title: dict | None = None
    url: str | None = Field(None, max_length=2048)
    description: dict | None = None
    image_url: str | None = Field(None, max_length=2048)
    is_active: bool | None = None
    sort_order: int | None = None
