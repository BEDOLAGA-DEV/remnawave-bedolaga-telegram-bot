"""Public site-verification endpoints used by payment-provider crawlers.

Antilopay (lk.antilopay.com) requires merchants to prove ownership of the
site that hosts the cabinet. The merchant copies a `apay-tag` value from
their Antilopay project page; we then either:

- expose it as `<meta name="apay-tag" content="...">` in `<head>` of the
  cabinet SPA (the React app reads `/cabinet/public/site-verification`
  and injects the tag at runtime), OR
- serve it as plain text at `/apay-meta-file.txt` so the file-based
  verification works without JavaScript.

Both endpoints are intentionally UNAUTHENTICATED — Antilopay's crawler
has no cabinet credentials.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings


router = APIRouter(prefix='/public', tags=['Cabinet:Public'])


def _resolved_apay_tag() -> str:
    """Return the configured apay-tag value, trimmed; empty string when unset."""
    return (settings.ANTILOPAY_APAY_VERIFICATION_TAG or '').strip()


@router.get('/site-verification', summary='Site verification tags for payment providers')
async def get_site_verification() -> dict[str, str | None]:
    """Return all configured site-verification tokens.

    The cabinet SPA pulls this on bootstrap to inject the relevant
    `<meta>` tags into the document head.
    """
    apay_tag = _resolved_apay_tag()
    return {
        # Empty string is normalized to None so the frontend can skip rendering.
        'apay_tag': apay_tag or None,
    }
