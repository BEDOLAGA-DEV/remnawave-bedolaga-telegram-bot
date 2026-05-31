"""Schemas for partner promo admin endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.crud.partner_promo import _is_safe_url


def _validate_https(value: str | None) -> str | None:
    # Defence-in-depth: reject non-https at the schema boundary (422), in addition
    # to the CRUD-layer guard. None passes (optional / not-provided).
    if value is None:
        return value
    if not _is_safe_url(value):
        raise ValueError('must be a valid https:// URL')
    return value


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

    @field_validator('url')
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not _is_safe_url(v):
            raise ValueError('url must be a valid https:// URL')
        return v

    @field_validator('image_url')
    @classmethod
    def _check_image_url(cls, v: str | None) -> str | None:
        if v in (None, ''):
            return None
        return _validate_https(v)


class PartnerPromoUpdateRequest(BaseModel):
    """Request to update a partner promo (all fields optional)."""

    title: dict | None = None
    url: str | None = Field(None, max_length=2048)
    description: dict | None = None
    image_url: str | None = Field(None, max_length=2048)
    is_active: bool | None = None
    sort_order: int | None = None

    @field_validator('url')
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        # url is NOT NULL in DB; if provided it must be https. None = not updating.
        return _validate_https(v)

    @field_validator('image_url')
    @classmethod
    def _check_image_url(cls, v: str | None) -> str | None:
        if v in (None, ''):
            return v
        return _validate_https(v)
