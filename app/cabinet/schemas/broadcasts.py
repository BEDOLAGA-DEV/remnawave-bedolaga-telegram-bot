"""Pydantic schemas for cabinet broadcasts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ============ Channel Types ============

BroadcastChannel = Literal['telegram', 'email', 'both']


# ============ Filters ============


class BroadcastFilter(BaseModel):
    """Single broadcast filter."""

    key: str
    label: str
    count: int | None = None
    group: str | None = None  # basic, subscription, traffic, registration, source, activity


class TariffFilter(BaseModel):
    """Tariff-based filter."""

    key: str  # tariff_1, tariff_2, ...
    label: str  # tariff name
    tariff_id: int
    count: int


class BroadcastFiltersResponse(BaseModel):
    """Response with all available filters."""

    filters: list[BroadcastFilter]  # basic filters
    tariff_filters: list[TariffFilter]  # tariff filters
    custom_filters: list[BroadcastFilter]  # custom filters


# ============ Tariffs ============


class TariffForBroadcast(BaseModel):
    """Tariff info for broadcast filtering."""

    id: int
    name: str
    filter_key: str  # tariff_{id}
    active_users_count: int


class BroadcastTariffsResponse(BaseModel):
    """Response with tariffs for filtering."""

    tariffs: list[TariffForBroadcast]


# ============ Buttons ============


class BroadcastButton(BaseModel):
    """Single broadcast button."""

    key: str
    label: str
    default: bool = False


class BroadcastButtonsResponse(BaseModel):
    """Response with available buttons."""

    buttons: list[BroadcastButton]


class CustomBroadcastButton(BaseModel):
    """Custom button for broadcast message."""

    label: str = Field(..., min_length=1, max_length=64)
    action_type: Literal['callback', 'url'] = 'callback'
    action_value: str = Field(..., min_length=1, max_length=256)

    @field_validator('action_value')
    @classmethod
    def validate_action_value(cls, v: str, info) -> str:
        action_type = info.data.get('action_type', 'callback')
        if action_type == 'url':
            if not v.startswith(('https://', 'tg://')):
                raise ValueError('URL must start with https:// or tg://')
        elif action_type == 'callback':
            # Telegram API limits callback_data to 64 bytes
            if len(v.encode('utf-8')) > 64:
                raise ValueError('Callback data must be at most 64 bytes')
        return v


# ============ Media ============


class BroadcastMediaRequest(BaseModel):
    """Media attachment for broadcast."""

    type: str = Field(..., pattern=r'^(photo|video|document)$')
    file_id: str
    caption: str | None = None


# ============ Create ============


class BroadcastCreateRequest(BaseModel):
    """Request to create a broadcast."""

    target: str
    message_text: str = Field(..., min_length=1, max_length=4000)
    selected_buttons: list[str] = Field(default_factory=lambda: ['home'])
    custom_buttons: list[CustomBroadcastButton] = Field(default_factory=list, max_length=10)
    media: BroadcastMediaRequest | None = None


# ============ Response ============


class BroadcastResponse(BaseModel):
    """Broadcast response."""

    id: int
    target_type: str
    message_text: str | None = None
    has_media: bool
    media_type: str | None = None
    media_file_id: str | None = None
    media_caption: str | None = None
    total_count: int
    sent_count: int
    failed_count: int
    blocked_count: int = 0
    status: str  # queued|in_progress|completed|partial|failed|cancelled|cancelling
    admin_id: int | None = None
    admin_name: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    progress_percent: float = 0.0

    # Email/channel fields
    channel: str = 'telegram'  # telegram|email|both
    email_subject: str | None = None
    email_html_content: str | None = None

    class Config:
        from_attributes = True


class BroadcastListResponse(BaseModel):
    """Paginated list of broadcasts."""

    items: list[BroadcastResponse]
    total: int
    limit: int
    offset: int


# ============ Preview ============


class BroadcastPreviewRequest(BaseModel):
    """Request to preview broadcast recipients count."""

    target: str


class BroadcastPreviewResponse(BaseModel):
    """Preview response with recipients count."""

    target: str
    count: int


# ============ Email Filters ============


class EmailFilterItem(BaseModel):
    """Single email filter with count."""

    key: str
    label: str
    count: int
    group: str | None = None


class EmailFiltersResponse(BaseModel):
    """Response with all email filters and their counts."""

    filters: list[EmailFilterItem]
    total_with_email: int


# ============ Combined Broadcast ============


class CombinedBroadcastCreateRequest(BaseModel):
    """Request to create a combined (telegram/email/both) broadcast."""

    channel: BroadcastChannel
    target: str

    # Telegram-specific fields
    message_text: str | None = Field(default=None, max_length=4000)
    selected_buttons: list[str] = Field(default_factory=lambda: ['home'])
    custom_buttons: list[CustomBroadcastButton] = Field(default_factory=list, max_length=10)
    media: BroadcastMediaRequest | None = None

    # Email-specific fields
    email_subject: str | None = Field(default=None, max_length=255)
    email_html_content: str | None = Field(default=None, max_length=100000)


# ============ Email Preview ============


class EmailPreviewRequest(BaseModel):
    """Request to preview email broadcast recipients."""

    target: str


class EmailPreviewResponse(BaseModel):
    """Preview response for email broadcast."""

    target: str
    count: int
