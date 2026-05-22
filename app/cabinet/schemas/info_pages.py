"""Schemas for info pages in cabinet."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class InfoPageResponse(BaseModel):
    """Full info page response."""

    id: int
    slug: str
    title: dict[str, str]
    content: dict[str, str]
    page_type: str = 'page'
    is_active: bool
    sort_order: int
    icon: str | None = None
    replaces_tab: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class InfoPageListItem(BaseModel):
    """Compact info page for list views."""

    id: int
    slug: str
    title: dict[str, str]
    page_type: str = 'page'
    is_active: bool
    sort_order: int
    icon: str | None = None
    replaces_tab: str | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class InfoPageCreateRequest(BaseModel):
    """Request to create an info page."""

    slug: str = Field(min_length=1, max_length=200, pattern=r'^[a-z0-9\-]+$')
    title: dict[str, str] = Field(default_factory=dict)
    content: dict[str, str] = Field(default_factory=dict)
    page_type: str = Field(default='page', pattern=r'^(page|faq)$')
    is_active: bool = True
    sort_order: int = 0
    icon: str | None = Field(None, max_length=50)
    replaces_tab: str | None = Field(None, pattern=r'^(faq|rules|privacy|offer)$')


class InfoPageUpdateRequest(BaseModel):
    """Request to update an info page."""

    slug: str | None = Field(None, min_length=1, max_length=200, pattern=r'^[a-z0-9\-]+$')
    title: dict[str, str] | None = None
    content: dict[str, str] | None = None
    page_type: str | None = Field(None, pattern=r'^(page|faq)$')
    is_active: bool | None = None
    sort_order: int | None = None
    icon: str | None = Field(None, max_length=50)
    replaces_tab: str | None = Field(None, pattern=r'^(faq|rules|privacy|offer)$')


class ReorderItem(BaseModel):
    """Single item in a reorder request."""

    id: int
    sort_order: int = Field(ge=0)


class ReorderRequest(BaseModel):
    """Request to bulk-reorder info pages."""

    items: list[ReorderItem] = Field(..., min_length=1)
