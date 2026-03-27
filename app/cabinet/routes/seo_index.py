"""Serves index.html with injected SEO meta tags for search engines and social previews.

Supports per-landing SEO: /buy/{slug} pages use meta_title/meta_description
from the landing_pages table. All other pages use global SEO settings.

Setup:
  1. Copy cabinet's index.html: docker cp cabinet:/usr/share/nginx/html/index.html data/cabinet_index.html
  2. Point nginx location / to this endpoint for non-asset requests
  3. Configure SEO settings via /cabinet/branding/seo API
  4. Pass X-Original-URI header from nginx for per-page SEO
"""

import html as html_module
import re
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import get_setting_value
from app.database.models import LandingPage

from ..dependencies import get_cabinet_db
from .branding import SEO_DESCRIPTION_KEY, SEO_KEYWORDS_KEY, SEO_OG_IMAGE_KEY, SEO_TITLE_KEY


logger = structlog.get_logger(__name__)

router = APIRouter(tags=['SEO'])

INDEX_PATH = Path('data/cabinet_index.html')

_cached_html: str | None = None
_TITLE_PLACEHOLDER = '<title>Loading...</title>'
_BUY_SLUG_RE = re.compile(r'^/buy/([a-zA-Z0-9_-]+)')


def _read_index() -> str:
    global _cached_html
    if _cached_html:
        return _cached_html
    _cached_html = INDEX_PATH.read_text(encoding='utf-8')
    return _cached_html


def _build_meta_block(title: str, description: str, og_image: str, keywords: str, url: str) -> str:
    t = html_module.escape(title)
    d = html_module.escape(description)
    lines = [f'<title>{t}</title>']
    if description:
        lines.append(f'<meta name="description" content="{d}" />')
    if keywords:
        lines.append(f'<meta name="keywords" content="{html_module.escape(keywords)}" />')
    lines.append(f'<meta property="og:title" content="{t}" />')
    if description:
        lines.append(f'<meta property="og:description" content="{d}" />')
    lines.append(f'<meta property="og:url" content="{html_module.escape(url)}" />')
    lines.append('<meta property="og:type" content="website" />')
    lines.append('<meta property="og:locale" content="ru_RU" />')
    if og_image:
        lines.append(f'<meta property="og:image" content="{html_module.escape(og_image)}" />')
    lines.append('<meta name="twitter:card" content="summary_large_image" />')
    lines.append(f'<meta name="twitter:title" content="{t}" />')
    if description:
        lines.append(f'<meta name="twitter:description" content="{d}" />')
    if og_image:
        lines.append(f'<meta name="twitter:image" content="{html_module.escape(og_image)}" />')
    return '\n    '.join(lines)


def _get_localized(json_field, lang: str = 'ru') -> str:
    if not json_field:
        return ''
    if isinstance(json_field, str):
        return json_field
    if isinstance(json_field, dict):
        return json_field.get(lang) or json_field.get('ru') or next(iter(json_field.values()), '')
    return ''


async def _get_landing_seo(db: AsyncSession, slug: str) -> dict | None:
    result = await db.execute(
        select(
            LandingPage.meta_title,
            LandingPage.meta_description,
            LandingPage.meta_keywords,
            LandingPage.meta_og_image,
        ).where(LandingPage.slug == slug, LandingPage.is_active.is_(True))
    )
    row = result.first()
    if not row:
        return None
    return {
        'title': _get_localized(row[0]),
        'description': _get_localized(row[1]),
        'keywords': _get_localized(row[2]),
        'og_image': row[3] or '',
    }


@router.get('/seo-index')
async def seo_index(request: Request, db: AsyncSession = Depends(get_cabinet_db)):
    """Serve index.html with SEO meta tags injected."""
    try:
        original = _read_index()
    except Exception:
        logger.exception('Failed to read cabinet index.html')
        return HTMLResponse('<html><body>Service unavailable</body></html>', status_code=502)

    base_url = (
        str(request.headers.get('x-forwarded-proto', 'https'))
        + '://'
        + str(request.headers.get('host', 'matrixvpn.top'))
    )
    request_path = request.headers.get('x-original-uri') or ''

    # Per-landing SEO for /buy/{slug}
    landing_match = _BUY_SLUG_RE.match(request_path)
    if landing_match:
        slug = landing_match.group(1)
        landing_seo = await _get_landing_seo(db, slug)
        if landing_seo:
            title = landing_seo['title'] or 'Cabinet'
            description = landing_seo['description']
            keywords = landing_seo['keywords']
            og_image = landing_seo['og_image'] or await get_setting_value(db, SEO_OG_IMAGE_KEY) or ''
            url = f'{base_url}/buy/{slug}'
            meta_block = _build_meta_block(title, description, og_image, keywords, url)
            injected = original.replace(_TITLE_PLACEHOLDER, meta_block)
            return HTMLResponse(injected, headers={'Cache-Control': 'no-cache, must-revalidate'})

    # Global SEO settings
    title = await get_setting_value(db, SEO_TITLE_KEY) or 'Cabinet'
    description = await get_setting_value(db, SEO_DESCRIPTION_KEY) or ''
    og_image = await get_setting_value(db, SEO_OG_IMAGE_KEY) or ''
    keywords = await get_setting_value(db, SEO_KEYWORDS_KEY) or ''

    meta_block = _build_meta_block(title, description, og_image, keywords, base_url)
    injected = original.replace(_TITLE_PLACEHOLDER, meta_block)

    return HTMLResponse(injected, headers={'Cache-Control': 'no-cache, must-revalidate'})


def invalidate_cache():
    global _cached_html
    _cached_html = None
