"""Support tickets schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


ALLOWED_MEDIA_TYPES = {'photo', 'video', 'document'}
MAX_MEDIA_ITEMS = 10


class TicketMediaItem(BaseModel):
    """Single media attachment in a ticket message."""

    type: str = Field(..., description='Media type: photo, video, or document')
    file_id: str = Field(..., max_length=255, description='Telegram file_id')
    caption: str | None = Field(None, max_length=1000, description='Optional caption')

    @model_validator(mode='after')
    def validate_type(self) -> 'TicketMediaItem':
        if self.type not in ALLOWED_MEDIA_TYPES:
            raise ValueError(f'type must be one of: {sorted(ALLOWED_MEDIA_TYPES)}')
        return self


class TicketMessageResponse(BaseModel):
    """Ticket message data."""

    id: int
    message_text: str
    is_from_admin: bool
    has_media: bool = False
    media_type: str | None = None
    media_file_id: str | None = None
    media_caption: str | None = None
    media_items: list[TicketMediaItem] | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class TicketResponse(BaseModel):
    """Ticket data."""

    id: int
    title: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    messages_count: int = 0
    last_message: TicketMessageResponse | None = None

    class Config:
        from_attributes = True


class TicketDetailResponse(BaseModel):
    """Ticket with all messages."""

    id: int
    title: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    is_reply_blocked: bool = False
    messages: list[TicketMessageResponse] = []

    class Config:
        from_attributes = True


class TicketListResponse(BaseModel):
    """Paginated ticket list."""

    items: list[TicketResponse]
    total: int
    page: int
    per_page: int
    pages: int


def _validate_media_bundle(
    media_type: str | None,
    media_file_id: str | None,
    media_items: list[TicketMediaItem] | None,
) -> None:
    """Shared validator for media-attached request bodies."""
    if media_items is not None:
        if len(media_items) == 0:
            raise ValueError('media_items must not be empty (send null instead)')
        if len(media_items) > MAX_MEDIA_ITEMS:
            raise ValueError(f'media_items cannot exceed {MAX_MEDIA_ITEMS} entries')
        # media_file_id/media_type on the top-level are kept as legacy back-compat
        # and must either be absent or match the first item.
        if media_file_id and media_file_id != media_items[0].file_id:
            raise ValueError('legacy media_file_id must match media_items[0].file_id')
        if media_type and media_type != media_items[0].type:
            raise ValueError('legacy media_type must match media_items[0].type')
        return

    # Legacy path: at most one media, and both or none of type/file_id
    if media_file_id and not media_type:
        raise ValueError('media_type is required when media_file_id is provided')
    if media_type and not media_file_id:
        raise ValueError('media_file_id is required when media_type is provided')
    if media_type and media_type not in ALLOWED_MEDIA_TYPES:
        raise ValueError(f'media_type must be one of: {sorted(ALLOWED_MEDIA_TYPES)}')


class TicketCreateRequest(BaseModel):
    """Request to create a new ticket."""

    title: str = Field(..., min_length=3, max_length=255, description='Ticket title')
    message: str = Field(default='', max_length=4000, description='Initial message')
    media_type: str | None = Field(None, description='Media type: photo, video, document')
    media_file_id: str | None = Field(None, description='Telegram file_id of uploaded media')
    media_caption: str | None = Field(None, max_length=1000, description='Media caption')
    media_items: list[TicketMediaItem] | None = Field(None, description='Multi-media attachments')

    @model_validator(mode='after')
    def validate_has_content(self) -> 'TicketCreateRequest':
        _validate_media_bundle(self.media_type, self.media_file_id, self.media_items)
        has_text = bool(self.message.strip())
        has_media = bool(self.media_file_id) or bool(self.media_items)
        if not has_text and not has_media:
            raise ValueError('message or media is required')
        return self


class TicketMessageCreateRequest(BaseModel):
    """Request to add message to ticket."""

    message: str = Field(default='', max_length=4000, description='Message text')
    media_type: str | None = Field(None, description='Media type: photo, video, document')
    media_file_id: str | None = Field(None, description='Telegram file_id of uploaded media')
    media_caption: str | None = Field(None, max_length=1000, description='Media caption')
    media_items: list[TicketMediaItem] | None = Field(None, description='Multi-media attachments')

    @model_validator(mode='after')
    def validate_has_content(self) -> 'TicketMessageCreateRequest':
        _validate_media_bundle(self.media_type, self.media_file_id, self.media_items)
        has_text = bool(self.message.strip())
        has_media = bool(self.media_file_id) or bool(self.media_items)
        if not has_text and not has_media:
            raise ValueError('message or media is required')
        return self
