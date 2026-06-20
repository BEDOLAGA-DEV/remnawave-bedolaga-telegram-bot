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
# Это проверка подписки на канал вызывается из пользовательских хендлеров. Длинный
# Telegram FloodWait (десятки секунд/минуты) нельзя «отсыпать» блокирующим sleep —
# юзер повиснет. Коротко (<= порога) ждём и повторяем; иначе считаем результат
# неопределённым (None: текущее состояние сохраняется, авто-деактивации НЕ будет).
_MAX_RETRY_AFTER = 5.0

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

    @staticmethod
    def should_disable_subscription(channel: dict, is_trial: bool) -> bool:
        """Check if a channel's settings require subscription deactivation.

        Respects both global and per-channel settings:
        - Global CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE=False overrides per-channel for trials
        - Per-channel disable_trial_on_leave / disable_paid_on_leave for fine-grained control
        """
        from app.config import settings

        if is_trial:
            if not settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE:
                return False
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
                # Reuse the sub_map from layer 2 above so we can fall back to the
                # last known DB value when an API call returns None (uncertain).
                sub_map_local = locals().get('sub_map', {})
                for ch in channels_needing_api:
                    check_result = await self._rate_limited_check(telegram_id, ch['channel_id'])
                    if check_result is None:
                        # Uncertain — keep last known DB value if any, otherwise
                        # default to True so a transient API hiccup never punishes
                        # a paying user (Telegram bug #313502).
                        last_known = sub_map_local.get(ch['channel_id'])
                        is_member = last_known.is_member if last_known else True
                        result[ch['channel_id']] = is_member
                        # Do NOT persist the uncertain value — let the next check
                        # try again with fresh API state.
                        continue
                    result[ch['channel_id']] = check_result
                    # Write DB first (source of truth), then cache
                    await upsert_user_channel_sub(db, telegram_id, ch['channel_id'], check_result)
                    await ChannelSubCache.set_sub_status(telegram_id, ch['channel_id'], check_result)
                await db.commit()
        elif channels_needing_api:
            # No bot available (e.g., cabinet API context). Fall back to the last
            # known DB value to avoid revoking access from paying users when the
            # bot singleton hasn't been initialised yet for this request.
            logger.warning(
                'No bot instance for API check -- using last known DB value (paid users keep access)',
                telegram_id=telegram_id,
                channels=[ch['channel_id'] for ch in channels_needing_api],
            )
            sub_map_local = locals().get('sub_map', {})
            for ch in channels_needing_api:
                last_known = sub_map_local.get(ch['channel_id'])
                result[ch['channel_id']] = last_known.is_member if last_known else True

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

    async def _rate_limited_check(self, telegram_id: int, channel_id: str) -> bool | None:
        """Check subscription via Telegram API with rate-limiting.

        Returns:
          - True  — confirmed member
          - False — confirmed non-member (BadRequest user_not_found, Forbidden, etc.)
          - None  — could not determine (network blip, double rate-limit, generic exception)

        The tri-state result is critical for paid subscriptions: a single transient
        TelegramNetworkError used to be treated as "not subscribed", which then got
        persisted to DB and triggered deactivate_subscription on the user's annual
        paid sub (Telegram bug report #313502 — Chara Freedom). Callers that auto-
        deactivate subs on `False` must now skip on `None` so a transient API hiccup
        no longer revokes paid access.
        """
        async with _API_SEMAPHORE:
            try:
                member = await self.bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
                await asyncio.sleep(_API_DELAY)
                return member.status in GOOD_STATUSES
            except TelegramRetryAfter as e:
                logger.warning('Rate limited by Telegram', retry_after=e.retry_after, channel_id=channel_id)
                if e.retry_after > _MAX_RETRY_AFTER:
                    # Слишком долгий FloodWait — не вешаем хендлер; результат неизвестен.
                    logger.warning(
                        'Telegram rate-limit too long, skipping channel check',
                        retry_after=e.retry_after,
                        channel_id=channel_id,
                    )
                    return None
                await asyncio.sleep(e.retry_after)
                try:
                    member = await self.bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
                    return member.status in GOOD_STATUSES
                except Exception:
                    logger.error('Double failure after rate-limit retry', channel_id=channel_id)
                    return None  # Uncertain — preserve last known value, do not auto-deactivate
            except TelegramForbiddenError:
                logger.critical(
                    'Bot removed/blocked from channel -- treating result as uncertain so '
                    'paid subs are not deactivated wholesale until the operator fixes access',
                    channel_id=channel_id,
                )
                return None  # Uncertain — bot's fault, not the user's
            except TelegramBadRequest as e:
                err_msg = str(e).lower()
                # Expected non-membership signals — обрабатываем тихо. Эти ошибки
                # означают «юзер просто не подписан»: либо никогда не был в канале,
                # либо вышел, либо удалил Telegram-аккаунт, либо был забанен в канале.
                # Раньше 'member not found' проваливался в logger.error и через
                # TelegramNotifierProcessor шёл в админ-чат каждый polling-cycle
                # (час) — спамил без причины.
                if (
                    'user not found' in err_msg
                    or 'member not found' in err_msg
                    or 'participant_id_invalid' in err_msg
                    or 'chat not found' in err_msg
                    or 'user is deactivated' in err_msg
                    or 'user_deactivated' in err_msg
                ):
                    logger.debug(
                        'User not a channel member (expected)',
                        channel_id=channel_id,
                        telegram_id=telegram_id,
                        reason=err_msg[:80],
                    )
                    return False
                logger.error('Bad request checking channel', channel_id=channel_id, error=str(e))
                return None  # Uncertain — unknown BadRequest, don't punish the user
            except TelegramNetworkError:
                logger.warning('Network error checking channel', channel_id=channel_id)
                return None  # Uncertain — transient, the next check will probably succeed
            except Exception as e:
                logger.error('Unexpected error checking channel', channel_id=channel_id, error=str(e))
                return None  # Uncertain — same logic, do not deactivate on unknown failures


# Singleton instance (bot is set at startup)
channel_subscription_service = ChannelSubscriptionService()
