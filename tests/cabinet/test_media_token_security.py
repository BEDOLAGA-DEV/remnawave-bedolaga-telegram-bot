"""Security: media download requires a valid, unexpired, file-bound signed token.

A leaked raw Telegram file_id must not be downloadable on its own — the URL is
signed (HMAC over file_id+exp with the cabinet JWT secret) and expires. Tokens
are minted only inside authenticated, owner-scoped ticket responses.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.media import (
    _media_signature,
    _verify_media_token,
    download_media,
    make_media_token,
)


FID = 'BAADAgADabcdef_-1234567890'
OTHER = 'BQADdifferent_-9876543210zy'


def test_token_roundtrip() -> None:
    assert _verify_media_token(FID, make_media_token(FID)) is True


def test_token_is_bound_to_file_id() -> None:
    assert _verify_media_token(OTHER, make_media_token(FID)) is False


def test_token_rejects_tampered_and_garbage() -> None:
    tok = make_media_token(FID)
    exp, _, sig = tok.partition('.')
    assert _verify_media_token(FID, f'{exp}.{sig[:-1]}{"0" if sig[-1] != "0" else "1"}') is False
    assert _verify_media_token(FID, '') is False
    assert _verify_media_token(FID, 'notatoken') is False


def test_token_rejects_expired() -> None:
    exp = int(time.time()) - 10
    assert _verify_media_token(FID, f'{exp}.{_media_signature(FID, exp)}') is False


@pytest.mark.asyncio
async def test_download_rejects_missing_token() -> None:
    # No token -> 404 before the bot is ever touched (the open-proxy is closed).
    with pytest.raises(HTTPException) as exc:
        await download_media(file_id=FID, token='')
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND
