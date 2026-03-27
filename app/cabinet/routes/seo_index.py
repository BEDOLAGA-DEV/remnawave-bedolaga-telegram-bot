"""Serves index.html with injected SEO meta tags for search engines and social previews.

Reads the cabinet's built index.html from disk (data/cabinet_index.html),
replaces <title>Loading...</title> with configured SEO meta tags,
and returns the modified HTML. This ensures search engines and social media
crawlers see proper title, description, and Open Graph tags instead of
the SPA's default "Loading..." placeholder.

Setup:
  1. Copy cabinet's index.html: docker cp cabinet:/usr/share/nginx/html/index.html data/cabinet_index.html
  2. Point nginx location / to this endpoint for non-asset requests
  3. Configure SEO settings via /cabinet/branding/seo API
"""

import html as html_module
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import get_setting_value

from ..dependencies import get_cabinet_db
from .branding import SEO_DESCRIPTION_KEY, SEO_KEYWORDS_KEY, SEO_OG_IMAGE_KEY, SEO_TITLE_KEY


logger = structlog.get_logger(__name__)

router = APIRouter(tags=['SEO'])

INDEX_PATH = Path('data/cabinet_index.html')

_cached_html: str | None = None


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


@router.get('/seo-index')
async def seo_index(request: Request, db: AsyncSession = Depends(get_cabinet_db)):
    """Serve index.html with SEO meta tags injected."""
    try:
        original = _read_index()
    except Exception:
        logger.exception('Failed to read cabinet index.html')
        return HTMLResponse('<html><body>Service unavailable</body></html>', status_code=502)


    title = await get_setting_value(db, SEO_TITLE_KEY) or 'Cabinet'
    description = await get_setting_value(db, SEO_DESCRIPTION_KEY) or ''
    og_image = await get_setting_value(db, SEO_OG_IMAGE_KEY) or ''
    keywords = await get_setting_value(db, SEO_KEYWORDS_KEY) or ''

    url = str(request.headers.get('x-forwarded-proto', 'https')) + '://' + str(request.headers.get('host', 'localhost'))

    meta_block = _build_meta_block(title, description, og_image, keywords, url)
    injected = original.replace('<title>Loading...</title>', meta_block)

    return HTMLResponse(injected, headers={'Cache-Control': 'no-cache, must-revalidate'})


def invalidate_cache():
    """Call after cabinet redeploy to refresh cached HTML."""
    global _cached_html
    _cached_html = None
