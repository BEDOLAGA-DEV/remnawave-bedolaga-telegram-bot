from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TicketMediaItemResponse(BaseModel):
    type: str
    file_id: str
    caption: str | None = None


class TicketMessageResponse(BaseModel):
    id: int
    user_id: int
    message_text: str
    is_from_admin: bool
    has_media: bool
    media_type: str | None = None
    media_file_id: str | None = None
    media_caption: str | None = None
    media_items: list[TicketMediaItemResponse] | None = None
    created_at: datetime


class TicketResponse(BaseModel):
    id: int
    user_id: int
    title: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    user_reply_block_permanent: bool
    user_reply_block_until: datetime | None = None
    messages: list[TicketMessageResponse] = Field(default_factory=list)


class TicketStatusUpdateRequest(BaseModel):
    status: str


class TicketPriorityUpdateRequest(BaseModel):
    priority: str


class TicketReplyBlockRequest(BaseModel):
    permanent: bool = False
    until: datetime | None = None


class TicketReplyRequest(BaseModel):
    message_text: str | None = Field(default=None, max_length=4000)
    media_type: str | None = Field(
        default=None,
        description='Тип медиа (photo, video, document, voice и т.д.)',
        max_length=32,
    )
    media_file_id: str | None = Field(default=None, max_length=255)
    media_caption: str | None = Field(default=None, max_length=4000)


class TicketReplyResponse(BaseModel):
    ticket: TicketResponse
    message: TicketMessageResponse


class TicketMediaResponse(BaseModel):
    id: int
    ticket_id: int
    media_type: str
    media_file_id: str
    media_caption: str | None = None
    media_url: str | None = None
