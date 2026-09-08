from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.localization.texts import get_texts


class TokenResponse(BaseModel):
    id: int
    name: str
    prefix: str = Field(
        ...,
        description=get_texts().t('API_TOKEN_PREFIX_DESCRIPTION', 'Первые символы токена для идентификации'),
    )
    description: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    last_used_ip: str | None = None
    created_by: str | None = None


class TokenCreateRequest(BaseModel):
    name: str
    description: str | None = None
    expires_at: datetime | None = None


class TokenCreateResponse(TokenResponse):
    token: str = Field(
        ...,
        description=get_texts().t('API_TOKEN_VALUE_DESCRIPTION', 'Полное значение токена (возвращается один раз)'),
    )
