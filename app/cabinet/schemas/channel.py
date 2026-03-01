"""Pydantic v2 schemas for channel subscription management."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.crud.required_channel import validate_channel_id as _validate_channel_id_format


def _validate_channel_link_value(v: str | None) -> str | None:
    """Shared channel_link validation: t.me URL, @username auto-convert, http->https upgrade."""
    if v is None:
        return v
    v = v.strip()
    if v.startswith('http://t.me/'):
        v = v.replace('http://', 'https://', 1)
    if v.startswith('https://t.me/'):
        return v
    if v.startswith('@'):
        return f'https://t.me/{v[1:]}'
    raise ValueError('channel_link must be a t.me URL or @username')


class ChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: str
    channel_link: str | None
    title: str | None
    is_active: bool
    sort_order: int
    disable_trial_on_leave: bool
    disable_paid_on_leave: bool


class ChannelListResponse(BaseModel):
    items: list[ChannelResponse]
    total: int


class ChannelCreateRequest(BaseModel):
    channel_id: str
    channel_link: str | None = None
    title: str | None = Field(None, max_length=255)
    disable_trial_on_leave: bool = True
    disable_paid_on_leave: bool = False

    @field_validator('channel_id')
    @classmethod
    def validate_channel_id(cls, v: str) -> str:
        return _validate_channel_id_format(v)

    @field_validator('channel_link')
    @classmethod
    def validate_channel_link(cls, v: str | None) -> str | None:
        return _validate_channel_link_value(v)


class ChannelUpdateRequest(BaseModel):
    channel_id: str | None = None
    channel_link: str | None = None
    title: str | None = Field(None, max_length=255)
    is_active: bool | None = None
    sort_order: int | None = None
    disable_trial_on_leave: bool | None = None
    disable_paid_on_leave: bool | None = None

    @field_validator('channel_id')
    @classmethod
    def validate_channel_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_channel_id_format(v)

    @field_validator('channel_link')
    @classmethod
    def validate_channel_link(cls, v: str | None) -> str | None:
        return _validate_channel_link_value(v)


class ChannelSubscriptionStatus(BaseModel):
    channel_id: str
    channel_link: str | None
    title: str | None
    is_subscribed: bool
