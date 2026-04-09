"""Schemas for news media upload responses."""

from typing import Literal

from pydantic import BaseModel


class NewsMediaUploadResponse(BaseModel):
    """Response returned after a successful media upload."""

    url: str
    thumbnail_url: str | None = None
    media_type: Literal['image', 'video']
    filename: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
