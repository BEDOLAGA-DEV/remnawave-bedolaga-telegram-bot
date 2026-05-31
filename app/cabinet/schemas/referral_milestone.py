"""Schemas for referral milestone admin endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_VALID_REWARD_TYPES = ('balance', 'promo_group')


class ReferralMilestoneResponse(BaseModel):
    """Full referral milestone response."""

    id: int
    threshold: int
    reward_type: str
    reward_value: int
    title: dict
    is_active: bool
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ReferralMilestoneCreateRequest(BaseModel):
    """Request to create a referral milestone."""

    threshold: int = Field(ge=1)
    reward_type: str
    reward_value: int = Field(ge=0)
    title: dict = Field(default_factory=dict)
    is_active: bool = True

    @field_validator('reward_type')
    @classmethod
    def _check_reward_type(cls, v: str) -> str:
        if v not in _VALID_REWARD_TYPES:
            raise ValueError(f'reward_type must be one of {_VALID_REWARD_TYPES}')
        return v


class ReferralMilestoneUpdateRequest(BaseModel):
    """Request to update a referral milestone (all fields optional)."""

    threshold: int | None = Field(None, ge=1)
    reward_type: str | None = None
    reward_value: int | None = Field(None, ge=0)
    title: dict | None = None
    is_active: bool | None = None

    @field_validator('reward_type')
    @classmethod
    def _check_reward_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in _VALID_REWARD_TYPES:
            raise ValueError(f'reward_type must be one of {_VALID_REWARD_TYPES}')
        return v
