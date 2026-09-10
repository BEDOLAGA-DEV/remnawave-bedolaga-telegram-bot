"""Pydantic schemas for cabinet pinned messages."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PinnedMessageMedia(BaseModel):
    type: str = Field(pattern=r'^(photo|video)$')
    file_id: str = Field(..., min_length=1, max_length=255)


class PinnedMessageCreateRequest(BaseModel):
    # Текст может быть пустым у сообщения из одной картинки: «текст или медиа»
    # проверяет роут, здесь только предел длины — тот же, что на правке.
    content: str = Field(..., max_length=4000)
    media: PinnedMessageMedia | None = None
    send_before_menu: bool = True
    send_on_every_start: bool = True
    broadcast: bool = False


class PinnedMessageUpdateRequest(BaseModel):
    content: str | None = Field(None, max_length=4000)
    send_before_menu: bool | None = None
    send_on_every_start: bool | None = None
    media: PinnedMessageMedia | None = None


class PinnedMessageSettingsRequest(BaseModel):
    send_before_menu: bool | None = None
    send_on_every_start: bool | None = None


class PinnedMessageResponse(BaseModel):
    id: int
    content: str | None
    media_type: str | None = None
    media_file_id: str | None = None
    send_before_menu: bool
    send_on_every_start: bool
    is_active: bool
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime | None = None


class PinnedMessageBroadcastResponse(BaseModel):
    message: PinnedMessageResponse
    sent_count: int
    failed_count: int


class PinnedMessageUnpinResponse(BaseModel):
    unpinned_count: int
    failed_count: int
    was_active: bool


class PinnedMessageListResponse(BaseModel):
    items: list[PinnedMessageResponse]
    total: int
    limit: int
    offset: int
