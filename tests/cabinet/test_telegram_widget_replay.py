"""Security: a Telegram Login Widget payload is one-time-use and short-lived.

The widget hash is signed by Telegram (proves authenticity) but the payload can
travel in the redirect URL (browser history / referrer / access logs). Without a
replay guard + a tight freshness window, a captured payload would be a reusable
login/link credential. (initData keeps its longer window — a real Telegram
caching bug — but it rides in the request body, not the URL.)
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.account_linking import LinkTelegramRequest, link_telegram
from app.cabinet.routes.auth import auth_telegram_widget
from app.cabinet.schemas.auth import TelegramWidgetAuthRequest


H = 'a' * 64


@pytest.mark.asyncio
async def test_widget_login_is_one_time_and_24h() -> None:
    req = TelegramWidgetAuthRequest(id=123, first_name='A', auth_date=1700000000, hash=H)
    validate = MagicMock(return_value=True)
    replay = AsyncMock(return_value=True)  # already used -> must be rejected
    with ExitStack() as s:
        s.enter_context(patch('app.cabinet.routes.auth.get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(
            patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False))
        )
        s.enter_context(patch('app.cabinet.routes.auth.validate_telegram_login_widget', validate))
        s.enter_context(patch('app.cabinet.routes.auth.TokenReplayCache.is_token_replayed', replay))
        with pytest.raises(HTTPException) as exc:
            await auth_telegram_widget(request=req, raw_request=MagicMock(), db=AsyncMock())
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    replay.assert_awaited_once()
    assert validate.call_args.kwargs.get('max_age_seconds') == 86400  # tightened from 30 days


@pytest.mark.asyncio
async def test_widget_link_is_one_time_and_24h() -> None:
    req = LinkTelegramRequest(id=123, auth_date=1700000000, hash=H)
    validate = MagicMock(return_value=True)
    replay = AsyncMock(return_value=True)
    with ExitStack() as s:
        s.enter_context(patch('app.cabinet.routes.account_linking.get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.RateLimitCache.is_ip_rate_limited',
                AsyncMock(return_value=False),
            )
        )
        s.enter_context(patch('app.cabinet.routes.account_linking.validate_telegram_login_widget', validate))
        s.enter_context(patch('app.cabinet.routes.account_linking.TokenReplayCache.is_token_replayed', replay))
        with pytest.raises(HTTPException) as exc:
            await link_telegram(
                request=req,
                raw_request=MagicMock(),
                user=SimpleNamespace(id=1, telegram_id=None),
                db=AsyncMock(),
            )
    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    replay.assert_awaited_once()
    assert validate.call_args.kwargs.get('max_age_seconds') == 86400
