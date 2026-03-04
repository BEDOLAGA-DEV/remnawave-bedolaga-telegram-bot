"""Cabinet AI FAQ schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─── Медиа-вложения FAQ ───

class CabinetAIFaqMediaResponse(BaseModel):
    """Медиа-вложение FAQ-статьи."""

    id: int
    article_id: int
    media_type: str
    file_id: str
    caption: str | None = None
    tag: str
    created_at: datetime

    class Config:
        from_attributes = True


class CabinetAIFaqMediaCreateRequest(BaseModel):
    """Запрос на создание медиа-вложения."""

    file_id: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=20, description="photo | video | animation")
    tag: str = Field(min_length=1, max_length=50, description="Уникальный тег для ИИ")
    caption: str | None = Field(default=None, max_length=1024, description="Описание для ИИ")


# ─── FAQ-статьи ───

class CabinetAIFaqArticleResponse(BaseModel):
    """Ответ для статьи FAQ."""

    id: int
    title: str
    content: str
    keywords: str | None = None
    is_active: bool
    media: list[CabinetAIFaqMediaResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CabinetAIFaqArticleCreateRequest(BaseModel):
    """Запрос на создание статьи FAQ."""

    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    keywords: str | None = Field(default=None, max_length=1024)
    is_active: bool = True


class CabinetAIFaqArticleUpdateRequest(BaseModel):
    """Запрос на обновление статьи FAQ."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    keywords: str | None = Field(default=None, max_length=1024)
    is_active: bool | None = None
