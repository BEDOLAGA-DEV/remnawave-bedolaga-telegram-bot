"""Regression tests for the tri-state channel-check return type.

Telegram bug report #313502 — Chara Freedom
-------------------------------------------
A user with an annual paid subscription had their sub flipped to DISABLED
by `channel_checker.py` after Telegram returned a transient network error
on the membership check: `_rate_limited_check` lumped network errors with
genuine "user is not a member" and returned False, which the caller
persisted to DB and then `deactivate_subscription` ran.

Fix: `_rate_limited_check` now returns Optional[bool] — None means
"could not determine, do not punish the user". The caller treats None
as "keep last known DB value, do not write".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from app.services.channel_subscription_service import ChannelSubscriptionService


def _fake_member(status: str) -> MagicMock:
    m = MagicMock()
    m.status = status
    return m


@pytest.mark.asyncio
async def test_member_check_returns_true_when_user_is_member() -> None:
    svc = ChannelSubscriptionService(bot=AsyncMock())
    svc.bot.get_chat_member = AsyncMock(return_value=_fake_member('member'))
    assert await svc._rate_limited_check(123, '-100123') is True


@pytest.mark.asyncio
async def test_member_check_returns_false_on_confirmed_user_not_found() -> None:
    """A BadRequest with 'user not found' is a confirmed non-membership —
    the user really did leave (or never joined) the channel."""
    svc = ChannelSubscriptionService(bot=AsyncMock())
    svc.bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message='Bad Request: user not found')
    )
    assert await svc._rate_limited_check(123, '-100123') is False


@pytest.mark.asyncio
async def test_member_check_returns_none_on_network_error() -> None:
    """Transient network error must NOT be treated as 'not a member' —
    that's how Chara's paid annual sub got deactivated."""
    svc = ChannelSubscriptionService(bot=AsyncMock())
    svc.bot.get_chat_member = AsyncMock(side_effect=TelegramNetworkError(method=MagicMock(), message='read timeout'))
    assert await svc._rate_limited_check(123, '-100123') is None


@pytest.mark.asyncio
async def test_member_check_returns_none_on_bot_removed_from_channel() -> None:
    """Bot's own access loss is the operator's problem, not the user's —
    don't auto-deactivate every paid sub when the bot itself loses access."""
    svc = ChannelSubscriptionService(bot=AsyncMock())
    svc.bot.get_chat_member = AsyncMock(
        side_effect=TelegramForbiddenError(method=MagicMock(), message='bot kicked from channel')
    )
    assert await svc._rate_limited_check(123, '-100123') is None


@pytest.mark.asyncio
async def test_member_check_returns_none_on_unknown_bad_request() -> None:
    """Unrecognised BadRequest message — treat as uncertain rather than
    assuming the user left."""
    svc = ChannelSubscriptionService(bot=AsyncMock())
    svc.bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message='some new error message we have not seen')
    )
    assert await svc._rate_limited_check(123, '-100123') is None


@pytest.mark.asyncio
async def test_member_check_returns_none_on_double_rate_limit_failure() -> None:
    """Telegram is rate-limiting us hard — the user is not at fault."""
    svc = ChannelSubscriptionService(bot=AsyncMock())
    svc.bot.get_chat_member = AsyncMock(
        side_effect=[
            TelegramRetryAfter(method=MagicMock(), message='rate-limited', retry_after=0),
            TelegramNetworkError(method=MagicMock(), message='retry also failed'),
        ]
    )
    with patch('app.services.channel_subscription_service.asyncio.sleep', AsyncMock()):
        result = await svc._rate_limited_check(123, '-100123')
    assert result is None


@pytest.mark.asyncio
async def test_member_check_returns_none_on_generic_exception() -> None:
    """Any unexpected error keeps the user's access."""
    svc = ChannelSubscriptionService(bot=AsyncMock())
    svc.bot.get_chat_member = AsyncMock(side_effect=RuntimeError('something unexpected'))
    assert await svc._rate_limited_check(123, '-100123') is None


@pytest.mark.asyncio
async def test_check_user_subscriptions_preserves_last_known_on_uncertain() -> None:
    """Integration: when API check is uncertain (None), the public result
    must reflect the last known DB value — NOT False — so the middleware's
    `not is_subscribed` logic doesn't kick paying users out of their subs."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    svc = ChannelSubscriptionService(bot=AsyncMock())
    # Force the rate-limited path to return uncertain
    svc._rate_limited_check = AsyncMock(return_value=None)

    # DB layer has a stale-but-true record for this user/channel
    last_known = SimpleNamespace(
        is_member=True,
        checked_at=datetime(2020, 1, 1, tzinfo=UTC),  # stale → triggers API check
    )

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()

    async def fake_get_user_channel_subs(_db, _tg_id):
        return [SimpleNamespace(channel_id='-100123', is_member=True, checked_at=last_known.checked_at)]

    with (
        patch('app.services.channel_subscription_service.AsyncSessionLocal') as session_local,
        patch('app.services.channel_subscription_service.ChannelSubCache.get_sub_statuses', AsyncMock(return_value={})),
        patch('app.services.channel_subscription_service.ChannelSubCache.set_sub_status', AsyncMock()),
        patch(
            'app.services.channel_subscription_service.get_user_channel_subs',
            fake_get_user_channel_subs,
        ),
        patch('app.services.channel_subscription_service.upsert_user_channel_sub', AsyncMock()) as upsert_mock,
    ):
        session_local.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        session_local.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await svc._check_user_subscriptions_for_channels(
            123,
            [{'channel_id': '-100123', 'disable_paid_on_leave': True}],
        )

    # Last known value preserved despite API being uncertain
    assert result['-100123'] is True
    # And we never persisted the uncertain result
    upsert_mock.assert_not_called()
