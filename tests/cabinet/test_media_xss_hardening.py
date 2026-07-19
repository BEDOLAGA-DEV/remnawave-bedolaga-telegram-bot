"""Regression tests for stored-XSS hardening of cabinet ticket media.

User-uploaded ticket attachments are served from the cabinet's own origin. An
HTML/SVG file served inline as text/html / image/svg+xml would execute JS in the
app origin and steal the JWT/refresh token from localStorage. These tests pin the
safe serving contract: only raster images render inline; everything else downloads
as an opaque blob, with nosniff + a locked-down CSP, and a sanitized filename.
"""

from __future__ import annotations

import pytest

from app.cabinet.routes.media import (
    _BLOCKED_UPLOAD_CONTENT_TYPES,
    _BLOCKED_UPLOAD_EXTENSIONS,
    _content_response_params,
    _sanitize_download_filename,
)


@pytest.mark.parametrize('filename', ['x.jpg', 'x.jpeg', 'x.png', 'x.gif', 'x.webp'])
def test_raster_images_served_inline_with_their_type(filename):
    media_type, headers = _content_response_params(filename)
    assert media_type.startswith('image/')
    assert headers['Content-Disposition'].startswith('inline;')


@pytest.mark.parametrize(
    'filename',
    ['evil.html', 'evil.htm', 'evil.svg', 'evil.xml', 'evil.js', 'doc.pdf', 'archive.zip', 'noext'],
)
def test_non_raster_forced_to_download_as_octet_stream(filename):
    media_type, headers = _content_response_params(filename)
    # Never serve a renderable/scriptable content-type for these.
    assert media_type == 'application/octet-stream'
    assert headers['Content-Disposition'].startswith('attachment;')


def test_html_is_never_text_html():
    media_type, headers = _content_response_params('payload.html')
    assert media_type != 'text/html'
    assert 'attachment' in headers['Content-Disposition']


def test_svg_is_never_image_svg_xml():
    media_type, _ = _content_response_params('payload.svg')
    assert media_type != 'image/svg+xml'


def test_hardening_headers_always_present():
    for filename in ('photo.png', 'evil.html', 'doc.pdf'):
        _media_type, headers = _content_response_params(filename)
        assert headers['X-Content-Type-Options'] == 'nosniff'
        assert 'sandbox' in headers['Content-Security-Policy']
        assert "default-src 'none'" in headers['Content-Security-Policy']
        assert headers['Cache-Control'] == 'private, no-store'


def test_filename_sanitized_against_header_injection():
    # CRLF / quotes / path separators must not survive into the header.
    dirty = 'a/b\\c"d\r\ne.html'
    cleaned = _sanitize_download_filename(dirty)
    # The security-relevant chars (CRLF / quote / path separators) must be gone…
    assert '\r' not in cleaned and '\n' not in cleaned
    assert '"' not in cleaned and '/' not in cleaned and '\\' not in cleaned
    # …leaving only the basename's plain chars (separators split, the rest kept).
    assert cleaned == 'cde.html'


def test_empty_filename_falls_back():
    assert _sanitize_download_filename('') == 'file'
    assert _sanitize_download_filename('///') == 'file'


def test_blocked_upload_lists_cover_active_content():
    assert 'text/html' in _BLOCKED_UPLOAD_CONTENT_TYPES
    assert 'image/svg+xml' in _BLOCKED_UPLOAD_CONTENT_TYPES
    assert '.html' in _BLOCKED_UPLOAD_EXTENSIONS
    assert '.svg' in _BLOCKED_UPLOAD_EXTENSIONS
    assert '.js' in _BLOCKED_UPLOAD_EXTENSIONS
