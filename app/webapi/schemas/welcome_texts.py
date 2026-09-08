from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, validator

from app.localization.texts import get_texts


def _normalize_text(value: str) -> str:
    cleaned = (value or '').strip()
    if not cleaned:
        raise ValueError(get_texts().t('WELCOME_TEXT_EMPTY', 'Text cannot be empty'))
    return cleaned


class WelcomeTextResponse(BaseModel):
    id: int
    text: str
    is_active: bool
    is_enabled: bool
    created_by: int | None
    created_at: datetime
    updated_at: datetime


class WelcomeTextCreateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    is_enabled: bool = True
    is_active: bool = True

    _normalize_text = validator('text', allow_reuse=True)(_normalize_text)


class WelcomeTextUpdateRequest(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=4000)
    is_enabled: bool | None = None
    is_active: bool | None = None

    @validator('text')
    def validate_text(cls, value):
        if value is None:
            return value
        return _normalize_text(value)


class WelcomeTextListResponse(BaseModel):
    items: list[WelcomeTextResponse]
    total: int
    limit: int
    offset: int
