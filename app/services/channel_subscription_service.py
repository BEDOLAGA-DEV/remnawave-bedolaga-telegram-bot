"""Channel subscription verification service.

Architecture for 100k+ users:
1. ChatMemberUpdated events -> update PostgreSQL (source of truth) + Redis in real-time
2. Middleware reads ONLY from Redis/PostgreSQL (never calls Telegram API directly)
3. Background reconciliation (~10 req/sec) corrects drift
"""

import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from app.database.crud.required_channel import (
    get_active_channels,
    get_user_channel_subs,
    upsert_user_channel_sub,
)
from app.database.database import AsyncSessionLocal
from app.utils.cache import ChannelSubCache


logger = structlog.get_logger(__name__)

# Rate limiting for Telegram API calls
_API_SEMAPHORE = asyncio.Semaphore(20)  # max 20 concurrent getChatMember calls
_API_DELAY = 0.05  # 50ms between calls -> ~20/sec safe rate

GOOD_STATUSES = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)

# How long a DB record is considered fresh (no API call needed)
DB_FRESHNESS_SECONDS = 1800  # 30 min


class ChannelSubscriptionService:
    """Centralized service for channel subscription verification."""

    def __init__(self, bot: Bot | None = None):
        self.bot = bot

    # -- Public API ---------------------------------------------------------------

    async def get_required_channels(self) -> list[dict]:
        """Get the list of active required channels (cached)."""
        cached = await ChannelSubCache.get_required_channels()
        if cached is not None:
            return cached

        async with AsyncSessionLocal() as db:
            channels = await get_active_channels(db)
            result = [
                {
                    'id': ch.id,
                    'channel_id': ch.channel_id,
                    'channel_link': ch.channel_link,
                    'title': ch.title,
                    'sort_order': ch.sort_order,
                    'disable_trial_on_leave': ch.disable_trial_on_leave,
                    'disable_paid_on_leave': ch.disable_paid_on_leave,
                }
                for ch in channels
            ]
            await ChannelSubCache.set_required_channels(result)
            return result

    async def get_required_channel_ids(self) -> set[str]:
        """Get the set of active required channel_ids (for event filtering)."""
        channels = await self.get_required_channels()
        return {ch['channel_id'] for ch in channels}

    async def get_channel_settings(self, channel_id: str) -> dict | None:
        """Get per-channel settings for a specific channel (from cache)."""
        channels = await self.get_required_channels()
        for ch in channels:
            if ch['channel_id'] == channel_id:
                return ch
        return None

    def should_disable_subscription(self, channel: dict, is_trial: bool) -> bool:
        """Check if a channel's settings require subscription deactivation."""
        if is_trial:
            return channel.get('disable_trial_on_leave', True)
        return channel.get('disable_paid_on_leave', False)

    async def check_user_subscriptions(self, telegram_id: int) -> dict[str, bool]:
        """Check user subscriptions to all required channels.

        Returns {channel_id: is_member}.
        Does NOT call Telegram API unless cache miss + stale DB.
        Uses a SINGLE DB session for all channels (no N+1).
        """
        channels = await self.get_required_channels()
        return await self._check_user_subscriptions_for_channels(telegram_id, channels)

    async def _check_user_subscriptions_for_channels(
        self,
        telegram_id: int,
        channels: list[dict],
    ) -> dict[str, bool]:
        """Internal: check subscriptions for a given list of channels.

        Avoids double-fetching required_channels when called from
        get_unsubscribed_channels or get_channels_with_status.
        """
        if not channels:
            return {}

        result: dict[str, bool] = {}
        channels_needing_db: list[dict] = []

        # Layer 1: Redis cache (single MGET round-trip)
        all_channel_ids = [ch['channel_id'] for ch in channels]
        cached_statuses = await ChannelSubCache.get_sub_statuses(telegram_id, all_channel_ids)
        for ch in channels:
            channel_id = ch['channel_id']
            cached = cached_statuses.get(channel_id)
            if cached is not None:
                result[channel_id] = cached
            else:
                channels_needing_db.append(ch)

        # Layer 2: PostgreSQL (single session for all channels)
        channels_needing_api: list[dict] = []
        if channels_needing_db:
            async with AsyncSessionLocal() as db:
                subs = await get_user_channel_subs(db, telegram_id)
                sub_map = {s.channel_id: s for s in subs}

                for ch in channels_needing_db:
                    channel_id = ch['channel_id']
                    sub = sub_map.get(channel_id)

                    if sub and sub.checked_at:
                        age = (datetime.now(UTC) - sub.checked_at).total_seconds()
                        if age < DB_FRESHNESS_SECONDS:
                            result[channel_id] = sub.is_member
                            await ChannelSubCache.set_sub_status(telegram_id, channel_id, sub.is_member)
                            continue

                    channels_needing_api.append(ch)

        # Layer 3: Rate-limited API calls for channels without fresh data
        if channels_needing_api and self.bot:
            async with AsyncSessionLocal() as db:
                for ch in channels_needing_api:
                    is_member = await self._rate_limited_check(telegram_id, ch['channel_id'])
                    result[ch['channel_id']] = is_member
                    # Write DB first (source of truth), then cache
                    await upsert_user_channel_sub(db, telegram_id, ch['channel_id'], is_member)
                    await ChannelSubCache.set_sub_status(telegram_id, ch['channel_id'], is_member)
                await db.commit()
        elif channels_needing_api:
            # No bot available (e.g., cabinet API context) -- fail-closed
            logger.warning(
                'No bot instance for API check -- failing closed',
                telegram_id=telegram_id,
                channels=[ch['channel_id'] for ch in channels_needing_api],
            )
            for ch in channels_needing_api:
                result[ch['channel_id']] = False

        return result

    async def is_user_subscribed_to_all(self, telegram_id: int) -> bool:
        """Quick check: is user subscribed to ALL required channels?"""
        subs = await self.check_user_subscriptions(telegram_id)
        if not subs:
            return True  # No required channels = subscribed
        return all(subs.values())

    async def get_unsubscribed_channels(self, telegram_id: int) -> list[dict]:
        """Get the list of channels the user is NOT subscribed to."""
        channels = await self.get_required_channels()
        subs = await self._check_user_subscriptions_for_channels(telegram_id, channels)

        unsubscribed = []
        for ch in channels:
            if not subs.get(ch['channel_id'], False):
                unsubscribed.append(ch)
        return unsubscribed

    async def get_channels_with_status(self, telegram_id: int) -> list[dict]:
        """Get all required channels with per-channel subscription status (for cabinet API)."""
        channels = await self.get_required_channels()
        subs = await self._check_user_subscriptions_for_channels(telegram_id, channels)

        result = []
        for ch in channels:
            result.append(
                {
                    'channel_id': ch['channel_id'],
                    'channel_link': ch.get('channel_link'),
                    'title': ch.get('title'),
                    'is_subscribed': subs.get(ch['channel_id'], False),
                    'disable_trial_on_leave': ch.get('disable_trial_on_leave', True),
                    'disable_paid_on_leave': ch.get('disable_paid_on_leave', False),
                }
            )
        return result

    async def get_first_channel_id(self) -> str | None:
        """Get the first active channel ID (for announcements, contest posts, etc.).

        Channel IDs are always stored as strings in the DB.
        Telegram API accepts string channel_id in chat_id parameters.
        """
        channels = await self.get_required_channels()
        if not channels:
            return None
        return channels[0]['channel_id']

    # -- Event handlers (called from ChatMemberUpdated router) --------------------

    async def on_user_joined(self, telegram_id: int, channel_id: str) -> None:
        """Called when ChatMemberUpdated fires: user subscribed."""
        logger.info('Channel join event', telegram_id=telegram_id, channel_id=channel_id)
        # Write DB first (source of truth), then cache
        async with AsyncSessionLocal() as db:
            await upsert_user_channel_sub(db, telegram_id, channel_id, True)
            await db.commit()
        await ChannelSubCache.set_sub_status(telegram_id, channel_id, True)

    async def on_user_left(self, telegram_id: int, channel_id: str) -> None:
        """Called when ChatMemberUpdated fires: user unsubscribed."""
        logger.info('Channel leave event', telegram_id=telegram_id, channel_id=channel_id)
        # Write DB first (source of truth), then cache
        async with AsyncSessionLocal() as db:
            await upsert_user_channel_sub(db, telegram_id, channel_id, False)
            await db.commit()
        await ChannelSubCache.set_sub_status(telegram_id, channel_id, False)

    # -- Channel list management --------------------------------------------------

    async def invalidate_channels_cache(self) -> None:
        """Invalidate the channels list cache (call after CRUD)."""
        await ChannelSubCache.invalidate_channels()

    async def invalidate_user_cache(self, telegram_id: int) -> None:
        """Invalidate all cached subscription statuses for a user."""
        channels = await self.get_required_channels()
        channel_ids = [ch['channel_id'] for ch in channels]
        await ChannelSubCache.invalidate_user_channels(telegram_id, channel_ids)

    # -- Rate-limited Telegram API ------------------------------------------------

    async def _rate_limited_check(self, telegram_id: int, channel_id: str) -> bool:
        """Check subscription via Telegram API with rate-limiting.

        SECURITY: Fail-closed -- any error returns False (not subscribed).
        For a VPN access control system, false negatives (temporary denial)
        are preferable to false positives (unauthorized access).
        """
        async with _API_SEMAPHORE:
            try:
                member = await self.bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
                await asyncio.sleep(_API_DELAY)
                return member.status in GOOD_STATUSES
            except TelegramRetryAfter as e:
                logger.warning('Rate limited by Telegram', retry_after=e.retry_after, channel_id=channel_id)
                await asyncio.sleep(e.retry_after)
                try:
                    member = await self.bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
                    return member.status in GOOD_STATUSES
                except Exception:
                    logger.error('Double failure after rate-limit retry', channel_id=channel_id)
                    return False  # Fail-closed on double failure
            except TelegramForbiddenError:
                logger.critical(
                    'Bot removed/blocked from channel -- all checks will fail-closed',
                    channel_id=channel_id,
                )
                return False  # Fail-closed -- bot cannot verify membership
            except TelegramBadRequest as e:
                if 'user not found' in str(e).lower():
                    return False  # User never interacted with bot in that context
                logger.error('Bad request checking channel', channel_id=channel_id, error=str(e))
                return False  # Fail-closed
            except TelegramNetworkError:
                logger.warning('Network error checking channel', channel_id=channel_id)
                return False  # Fail-closed
            except Exception as e:
                logger.error('Unexpected error checking channel', channel_id=channel_id, error=str(e))
                return False  # Fail-closed


# Singleton instance (bot is set at startup)
channel_subscription_service = ChannelSubscriptionService()
