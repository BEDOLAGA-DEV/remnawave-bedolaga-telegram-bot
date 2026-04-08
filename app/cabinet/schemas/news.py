"""Schemas for news articles in cabinet.

Security notes:
- featured_image_url is validated to only accept http/https schemes.
- category_color is validated as a strict hex color (#RGB, #RRGGBB, etc.).
- Slug is sanitized to only allow [a-zA-Z0-9_-].
- Content is server-side sanitized to strip <script>, event handlers, and
  dangerous URI schemes as a defense-in-depth measure. The frontend also
  sanitizes via DOMPurify, but server-side sanitization protects against
  alternative consumers (mobile apps, RSS, email digests) and compromised
  frontends.
"""

import re
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Pre-compiled regex for hex color validation (reused across validators)
_HEX_COLOR_RE: re.Pattern[str] = re.compile(r'^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')

# Pre-compiled regex for collapsing repeated hyphens in slugs
_MULTI_HYPHEN_RE: re.Pattern[str] = re.compile(r'-+')

# Maximum slug length (matches DB column constraint)
_MAX_SLUG_LENGTH: int = 500

# Allowed URL schemes for user-supplied URLs (featured_image_url)
_SAFE_URL_SCHEMES: frozenset[str] = frozenset({'http', 'https'})

# Cyrillic-to-Latin transliteration map for slug generation
_TRANSLIT_MAP: dict[str, str] = {
    'а': 'a',
    'б': 'b',
    'в': 'v',
    'г': 'g',
    'д': 'd',
    'е': 'e',
    'ё': 'yo',
    'ж': 'zh',
    'з': 'z',
    'и': 'i',
    'й': 'y',
    'к': 'k',
    'л': 'l',
    'м': 'm',
    'н': 'n',
    'о': 'o',
    'п': 'p',
    'р': 'r',
    'с': 's',
    'т': 't',
    'у': 'u',
    'ф': 'f',
    'х': 'kh',
    'ц': 'ts',
    'ч': 'ch',
    'ш': 'sh',
    'щ': 'shch',
    'ъ': '',
    'ы': 'y',
    'ь': '',
    'э': 'e',
    'ю': 'yu',
    'я': 'ya',
}


def _slugify(title: str) -> str:
    """Generate a URL-safe slug from a title, transliterating Cyrillic."""
    slug = title.lower()
    result: list[str] = []
    for ch in slug:
        if ch in _TRANSLIT_MAP:
            result.append(_TRANSLIT_MAP[ch])
        elif ch.isascii() and (ch.isalnum() or ch in '-_'):
            result.append(ch)
        elif ch == ' ':
            result.append('-')
    slug = ''.join(result)
    slug = _MULTI_HYPHEN_RE.sub('-', slug).strip('-')
    return slug[:_MAX_SLUG_LENGTH] or 'untitled'


def _validate_hex_color(v: str) -> str:
    """Validate a hex color string. Raises ValueError on invalid input."""
    if not _HEX_COLOR_RE.match(v):
        msg = 'category_color must be a valid hex color (e.g. #00e5a0)'
        raise ValueError(msg)
    return v


def _validate_safe_url(v: str) -> str:
    """Validate that a URL uses http or https scheme only.

    Prevents javascript:, data:, vbscript:, and other dangerous URI schemes
    from being stored in the database and later rendered in <img> or <a> tags.
    """
    try:
        parsed = urlparse(v)
    except Exception:
        msg = 'Invalid URL format'
        raise ValueError(msg)

    if parsed.scheme not in _SAFE_URL_SCHEMES:
        msg = f'URL scheme must be http or https, got: {parsed.scheme!r}'
        raise ValueError(msg)

    if not parsed.netloc:
        msg = 'URL must have a valid host'
        raise ValueError(msg)

    return v


# --- Server-side HTML content sanitization ---
# Pre-compiled patterns for stripping the most dangerous HTML constructs.
# This is a defense-in-depth measure: the frontend also sanitizes via DOMPurify.
# Uses regex rather than a full HTML parser to avoid adding a new dependency.
# Strips: <script>, <style>, <object>, <embed>, <applet>, <base>, <form>,
# <link>, <meta> tags and all on* event handler attributes.
_DANGEROUS_TAGS_RE: re.Pattern[str] = re.compile(
    r'<\s*/?\s*(script|style|object|embed|applet|base|form|link(?:\s)|meta)\b[^>]*>',
    re.IGNORECASE | re.DOTALL,
)
# Match on* event handler attributes, e.g. onclick="...", onerror='...'
_EVENT_HANDLER_RE: re.Pattern[str] = re.compile(
    r'\s+on[a-z]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)',
    re.IGNORECASE,
)
# Match javascript:, vbscript:, data: in href/src attributes
_DANGEROUS_URI_RE: re.Pattern[str] = re.compile(
    r'((?:href|src)\s*=\s*["\'])\s*(javascript|vbscript|data)\s*:',
    re.IGNORECASE,
)


def _sanitize_html_content(html: str) -> str:
    """Strip dangerous HTML constructs from article content.

    This is NOT a replacement for DOMPurify on the frontend. It is a
    defense-in-depth layer that removes the most obvious XSS vectors
    at the storage boundary. A full HTML sanitizer (nh3, bleach) would
    be stronger, but this avoids adding a new dependency.
    """
    if not html:
        return html

    # 1. Remove dangerous tags and their content
    result = _DANGEROUS_TAGS_RE.sub('', html)

    # Also strip <script>...</script> content (tag + body)
    result = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', result, flags=re.IGNORECASE)
    result = re.sub(r'<style\b[^>]*>[\s\S]*?</style>', '', result, flags=re.IGNORECASE)

    # 2. Remove event handler attributes
    result = _EVENT_HANDLER_RE.sub('', result)

    # 3. Neutralize dangerous URI schemes in href/src
    result = _DANGEROUS_URI_RE.sub(r'\1about:', result)

    return result


class NewsArticleResponse(BaseModel):
    """Full news article response (detail view)."""

    id: int
    title: str
    slug: str
    content: str
    excerpt: str | None
    category: str
    category_color: str
    tag: str | None
    category_id: int | None = None
    tag_id: int | None = None
    featured_image_url: str | None
    is_published: bool
    is_featured: bool
    published_at: datetime | None
    read_time_minutes: int
    views_count: int
    author_name: str | None = None
    created_at: datetime
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class NewsArticleListItem(BaseModel):
    """Compact news article for list views."""

    id: int
    title: str
    slug: str
    excerpt: str | None
    category: str
    category_color: str
    tag: str | None
    category_id: int | None = None
    tag_id: int | None = None
    featured_image_url: str | None
    is_published: bool
    is_featured: bool
    published_at: datetime | None
    read_time_minutes: int
    views_count: int

    model_config = ConfigDict(from_attributes=True)


class NewsListResponse(BaseModel):
    """Paginated list of news articles."""

    items: list[NewsArticleListItem]
    total: int
    categories: list[str] = Field(default_factory=list)


class NewsCreateRequest(BaseModel):
    """Request to create a news article."""

    title: str = Field(..., min_length=1, max_length=500)
    slug: str | None = Field(None, min_length=1, max_length=500)
    content: str = Field(default='', max_length=500_000)
    excerpt: str | None = Field(None, max_length=1000)
    category: str = Field(..., min_length=1, max_length=100)
    category_color: str = Field(default='#00e5a0', max_length=20)
    tag: str | None = Field(None, max_length=50)
    category_id: int | None = None
    tag_id: int | None = None
    featured_image_url: str | None = Field(None, max_length=2000)
    is_published: bool = False
    is_featured: bool = False
    read_time_minutes: int = Field(default=1, ge=1, le=60)

    @field_validator('content')
    @classmethod
    def sanitize_content(cls, v: str) -> str:
        """Strip dangerous HTML from article content (defense-in-depth)."""
        return _sanitize_html_content(v)

    @field_validator('category_color')
    @classmethod
    def validate_hex_color(cls, v: str) -> str:
        return _validate_hex_color(v)

    @field_validator('featured_image_url')
    @classmethod
    def validate_featured_image_url(cls, v: str | None) -> str | None:
        """Reject javascript:, data:, and other dangerous URL schemes."""
        if v is not None:
            return _validate_safe_url(v)
        return v

    @model_validator(mode='before')
    @classmethod
    def auto_generate_slug(cls, data: dict) -> dict:  # type: ignore[type-arg]
        """Generate slug from title when not explicitly provided."""
        if isinstance(data, dict) and not data.get('slug'):
            title = data.get('title', '')
            data['slug'] = _slugify(title) if isinstance(title, str) else 'untitled'
        return data

    @field_validator('slug')
    @classmethod
    def sanitize_slug(cls, v: str | None) -> str | None:
        """Ensure slug contains only URL-safe characters, transliterating Cyrillic."""
        if v is not None:
            return _slugify(v)
        return v


class NewsUpdateRequest(BaseModel):
    """Request to update a news article."""

    title: str | None = Field(None, min_length=1, max_length=500)
    slug: str | None = Field(None, min_length=1, max_length=500)
    content: str | None = Field(None, max_length=500_000)
    excerpt: str | None = None
    category: str | None = Field(None, min_length=1, max_length=100)
    category_color: str | None = Field(None, max_length=20)
    tag: str | None = None
    category_id: int | None = None
    tag_id: int | None = None
    featured_image_url: str | None = Field(None, max_length=2000)
    is_published: bool | None = None
    is_featured: bool | None = None
    read_time_minutes: int | None = Field(None, ge=1, le=60)

    @field_validator('content')
    @classmethod
    def sanitize_content(cls, v: str | None) -> str | None:
        """Strip dangerous HTML from article content (defense-in-depth)."""
        if v is not None:
            return _sanitize_html_content(v)
        return v

    @field_validator('category_color')
    @classmethod
    def validate_hex_color(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_hex_color(v)
        return v

    @field_validator('featured_image_url')
    @classmethod
    def validate_featured_image_url(cls, v: str | None) -> str | None:
        """Reject javascript:, data:, and other dangerous URL schemes."""
        if v is not None:
            return _validate_safe_url(v)
        return v

    @field_validator('slug')
    @classmethod
    def sanitize_slug(cls, v: str | None) -> str | None:
        """Ensure slug contains only URL-safe characters, transliterating Cyrillic."""
        if v is not None:
            return _slugify(v)
        return v


class NewsToggleResponse(BaseModel):
    """Response after toggling publish/featured status."""

    id: int
    is_published: bool
    is_featured: bool
    published_at: datetime | None
