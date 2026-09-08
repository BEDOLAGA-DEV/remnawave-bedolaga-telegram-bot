from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, validator

from app.localization.texts import get_texts


def _normalize_text(value: str) -> str:
    cleaned = (value or '').strip()
    if not cleaned:
        raise ValueError(get_texts().t('USER_MESSAGE_TEXT_EMPTY', 'Message text cannot be empty'))
    return cleaned


class UserMessageResponse(BaseModel):
    id: int
    message_text: str
    is_active: bool
    sort_order: int
    created_by: int | None
    created_at: datetime
    updated_at: datetime


class UserMessageCreateRequest(BaseModel):
    message_text: str = Field(..., min_length=1, max_length=4000)
    is_active: bool = True
    sort_order: int = Field(0, ge=0)

    _normalize_message_text = validator('message_text', allow_reuse=True)(_normalize_text)


class UserMessageUpdateRequest(BaseModel):
    message_text: str | None = Field(None, min_length=1, max_length=4000)
    is_active: bool | None = None
    sort_order: int | None = Field(None, ge=0)

    @validator('message_text')
    def validate_message_text(cls, value):
        if value is None:
            return value
        return _normalize_text(value)


class UserMessageListResponse(BaseModel):
    items: list[UserMessageResponse]
    total: int
    limit: int
    offset: int
