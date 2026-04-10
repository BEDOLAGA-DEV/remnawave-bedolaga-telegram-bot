"""Support tickets schemas for cabinet."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class TicketMessageResponse(BaseModel):
    """Ticket message data."""

    id: int
    message_text: str
    is_from_admin: bool
    has_media: bool = False
    media_type: str | None = None
    media_file_id: str | None = None
    media_caption: str | None = None
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


class TicketCreateRequest(BaseModel):
    """Request to create a new ticket."""

    title: str = Field(..., min_length=3, max_length=255, description='Ticket title')
    message: str = Field(..., min_length=10, max_length=4000, description='Initial message')
    media_type: str | None = Field(None, description='Media type: photo, video, document')
    media_file_id: str | None = Field(None, description='Telegram file_id of uploaded media')
    media_caption: str | None = Field(None, max_length=1000, description='Media caption')


class TicketMessageCreateRequest(BaseModel):
    """Request to add message to ticket."""

    message: str = Field(default='', max_length=4000, description='Message text')
    media_type: str | None = Field(None, description='Media type: photo, video, document')
    media_file_id: str | None = Field(None, description='Telegram file_id of uploaded media')
    media_caption: str | None = Field(None, max_length=1000, description='Media caption')

    @model_validator(mode='after')
    def validate_has_content(self) -> 'TicketMessageCreateRequest':
        if not self.message.strip() and not self.media_file_id:
            raise ValueError('message or media_file_id is required')
        return self
