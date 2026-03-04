from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─── Медиа-вложения FAQ ───

class AIFaqMediaResponse(BaseModel):
    """Медиа-вложение FAQ-статьи."""
    id: int
    article_id: int
    media_type: str
    file_id: str
    caption: str | None = None
    tag: str
    created_at: datetime


class AIFaqMediaCreateRequest(BaseModel):
    """Запрос на создание медиа-вложения (file_id получается через POST /upload)."""
    file_id: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=20, description="photo | video | animation")
    tag: str = Field(min_length=1, max_length=50, description="Уникальный тег для ИИ, например 'setup_android'")
    caption: str | None = Field(default=None, max_length=1024, description="Описание для ИИ")


# ─── FAQ-статьи ───

class AIFaqArticleResponse(BaseModel):
    id: int
    title: str
    content: str
    keywords: str | None = None
    is_active: bool
    media: list[AIFaqMediaResponse] = []
    created_at: datetime
    updated_at: datetime


class AIFaqArticleCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    keywords: str | None = Field(default=None, max_length=1024)
    is_active: bool = True


class AIFaqArticleUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    keywords: str | None = Field(default=None, max_length=1024)
    is_active: bool | None = None
