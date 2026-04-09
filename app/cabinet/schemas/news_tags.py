"""Schemas for news tags."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


_HEX_COLOR_RE: re.Pattern[str] = re.compile(r'^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')


class NewsTagCreate(BaseModel):
    """Request to create a news tag."""

    name: str = Field(..., min_length=1, max_length=50)
    color: str = Field(default='#94a3b8', max_length=20)

    @field_validator('color')
    @classmethod
    def validate_color(cls, v: str) -> str:
        if not _HEX_COLOR_RE.match(v):
            msg = 'Invalid hex color'
            raise ValueError(msg)
        return v


class NewsTagUpdate(BaseModel):
    """Request to update a news tag."""

    name: str | None = Field(None, min_length=1, max_length=50)
    color: str | None = Field(None, max_length=20)

    @field_validator('color')
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is not None and not _HEX_COLOR_RE.match(v):
            msg = 'Invalid hex color'
            raise ValueError(msg)
        return v


class NewsTagResponse(BaseModel):
    """News tag response."""

    id: int
    name: str
    color: str

    model_config = ConfigDict(from_attributes=True)
