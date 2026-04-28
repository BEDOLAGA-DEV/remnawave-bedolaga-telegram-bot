"""Schemas for help/FAQ articles in cabinet.

Security notes:
- category_color is validated as a strict hex color (#RGB, #RRGGBB, etc.).
- Slug is sanitized to only allow [a-zA-Z0-9_-] (transliterates Cyrillic).
- Content is server-side sanitized to strip <script>, event handlers, and
  dangerous URI schemes as a defense-in-depth measure (same policy as news).
- Locale is restricted to the cabinet's supported language codes.
"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .news import _sanitize_html_content, _slugify, _validate_hex_color


# Supported locales — keep in sync with src/locales/* folder in the SPA
_SUPPORTED_LOCALES: frozenset[str] = frozenset({'ru', 'en', 'fa', 'zh'})

# Pre-compiled emoji/icon validator — allow emoji (any unicode codepoint above
# basic ASCII) plus short icon slugs (e.g. "book", "help-circle")
_ICON_RE: re.Pattern[str] = re.compile(r'^[a-zA-Z0-9_-]{1,32}$')


def _validate_icon(v: str | None) -> str | None:
    """Allow None, emoji (<= 8 chars, any unicode), or ASCII icon slug."""
    if v is None:
        return v
    v = v.strip()
    if not v:
        return None
    if len(v) > 32:
        msg = 'category_icon must be <= 32 characters'
        raise ValueError(msg)
    # Accept emoji / short unicode — only reject if it looks like a slug but with invalid characters
    if v.isascii() and not _ICON_RE.match(v):
        msg = 'category_icon ASCII slug must match [a-zA-Z0-9_-]{1,32}'
        raise ValueError(msg)
    return v


def _validate_locale(v: str) -> str:
    """Normalize and validate a locale code."""
    v = v.lower().strip()
    if v not in _SUPPORTED_LOCALES:
        msg = f'locale must be one of {sorted(_SUPPORTED_LOCALES)}'
        raise ValueError(msg)
    return v


class HelpArticleResponse(BaseModel):
    """Full help article response (detail view)."""

    id: int
    title: str
    slug: str
    content: str
    excerpt: str | None
    category: str
    category_icon: str | None
    category_color: str
    locale: str
    display_order: int
    is_published: bool
    is_featured: bool
    views_count: int
    helpful_count: int
    not_helpful_count: int
    author_name: str | None = None
    created_at: datetime
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class HelpArticleListItem(BaseModel):
    """Compact help article for list views."""

    id: int
    title: str
    slug: str
    excerpt: str | None
    category: str
    category_icon: str | None
    category_color: str
    locale: str
    display_order: int
    is_published: bool
    is_featured: bool
    views_count: int

    model_config = ConfigDict(from_attributes=True)


class HelpCategory(BaseModel):
    """Category metadata for public help listing."""

    name: str
    icon: str | None = None
    color: str = '#00e5a0'
    count: int = 0


class HelpListResponse(BaseModel):
    """Paginated list of help articles grouped by category."""

    items: list[HelpArticleListItem]
    total: int
    categories: list[HelpCategory] = Field(default_factory=list)


class HelpCreateRequest(BaseModel):
    """Admin request to create a help article."""

    title: str = Field(..., min_length=1, max_length=500)
    slug: str | None = Field(None, min_length=1, max_length=500)
    content: str = Field(default='', max_length=500_000)
    excerpt: str | None = Field(None, max_length=1000)
    category: str = Field(default='general', min_length=1, max_length=100)
    category_icon: str | None = Field(None, max_length=32)
    category_color: str = Field(default='#00e5a0', max_length=20)
    locale: str = Field(default='ru', max_length=10)
    display_order: int = Field(default=0, ge=0, le=10_000)
    is_published: bool = False
    is_featured: bool = False

    @field_validator('content')
    @classmethod
    def sanitize_content(cls, v: str) -> str:
        return _sanitize_html_content(v)

    @field_validator('category_color')
    @classmethod
    def validate_hex_color(cls, v: str) -> str:
        return _validate_hex_color(v)

    @field_validator('category_icon')
    @classmethod
    def validate_category_icon(cls, v: str | None) -> str | None:
        return _validate_icon(v)

    @field_validator('locale')
    @classmethod
    def validate_locale_code(cls, v: str) -> str:
        return _validate_locale(v)

    @model_validator(mode='before')
    @classmethod
    def auto_generate_slug(cls, data: dict) -> dict:  # type: ignore[type-arg]
        if isinstance(data, dict) and not data.get('slug'):
            title = data.get('title', '')
            data['slug'] = _slugify(title) if isinstance(title, str) else 'untitled'
        return data

    @field_validator('slug')
    @classmethod
    def sanitize_slug(cls, v: str | None) -> str | None:
        if v is not None:
            return _slugify(v)
        return v


class HelpUpdateRequest(BaseModel):
    """Admin request to update a help article. All fields are optional."""

    title: str | None = Field(None, min_length=1, max_length=500)
    slug: str | None = Field(None, min_length=1, max_length=500)
    content: str | None = Field(None, max_length=500_000)
    excerpt: str | None = None
    category: str | None = Field(None, min_length=1, max_length=100)
    category_icon: str | None = Field(None, max_length=32)
    category_color: str | None = Field(None, max_length=20)
    locale: str | None = Field(None, max_length=10)
    display_order: int | None = Field(None, ge=0, le=10_000)
    is_published: bool | None = None
    is_featured: bool | None = None

    @field_validator('content')
    @classmethod
    def sanitize_content(cls, v: str | None) -> str | None:
        if v is not None:
            return _sanitize_html_content(v)
        return v

    @field_validator('category_color')
    @classmethod
    def validate_hex_color(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_hex_color(v)
        return v

    @field_validator('category_icon')
    @classmethod
    def validate_category_icon(cls, v: str | None) -> str | None:
        return _validate_icon(v)

    @field_validator('locale')
    @classmethod
    def validate_locale_code(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_locale(v)
        return v

    @field_validator('slug')
    @classmethod
    def sanitize_slug(cls, v: str | None) -> str | None:
        if v is not None:
            return _slugify(v)
        return v


class HelpToggleResponse(BaseModel):
    """Response after toggling publish/featured status."""

    id: int
    is_published: bool
    is_featured: bool


class HelpfulVoteRequest(BaseModel):
    """Public vote on whether an article was helpful."""

    helpful: bool


class HelpfulVoteResponse(BaseModel):
    """Updated helpful counters after a vote."""

    id: int
    helpful_count: int
    not_helpful_count: int
