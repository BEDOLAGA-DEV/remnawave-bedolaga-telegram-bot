"""ChatMemberUpdated event handler for real-time channel subscription tracking.

KEY COMPONENT for scalability: the bot receives push notifications from Telegram
when users join/leave channels, instead of polling via getChatMember.
Requirement: bot must be admin in each required channel.

IMPORTANT: Events are FILTERED to only process required channels.
Without filtering, the bot would process events from ALL channels it admins.
"""

from datetime import UTC, datetime

import structlog
from aiogram import Bot, Router
from aiogram.filters import IS_MEMBER, IS_NOT_MEMBER, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from app.config import settings
from app.database.crud.subscription import deactivate_subscription, reactivate_subscription
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal
from app.database.models import SubscriptionStatus, UserStatus
from app.keyboards.inline import get_channel_sub_keyboard
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.channel_subscription_service import channel_subscription_service
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

router = Router(name='channel_member')


async def _is_required_channel(channel_id: str) -> bool:
    """Check if the channel_id is one of our required channels."""
    required_ids = await channel_subscription_service.get_required_channel_ids()
    return channel_id in required_ids


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_joined_channel(event: ChatMemberUpdated, bot: Bot) -> None:
    """User subscribed to a channel -- update cache and reactivate VPN if applicable."""
    user = event.new_chat_member.user
    channel_id = str(event.chat.id)  # Normalize int to str (DB stores string)

    # FILTER: Only process events for required channels
    if not await _is_required_channel(channel_id):
        return

    await channel_subscription_service.on_user_joined(user.id, channel_id)

    # Check if user is now subscribed to ALL required channels
    if not settings.CHANNEL_IS_REQUIRED_SUB:
        return

    is_all_subscribed = await channel_subscription_service.is_user_subscribed_to_all(user.id)
    if not is_all_subscribed:
        return  # Still missing some channels

    # Reactivate subscription if it was disabled due to channel unsubscribe
    async with AsyncSessionLocal() as db:
        try:
            db_user = await get_user_by_telegram_id(db, user.id)
            if not db_user or not db_user.subscription:
                return
            if db_user.status == UserStatus.BLOCKED.value:
                return

            subscription = db_user.subscription
            if subscription.status != SubscriptionStatus.DISABLED.value:
                return

            # Don't reactivate expired subscriptions
            if subscription.end_date and subscription.end_date <= datetime.now(UTC):
                return

            await reactivate_subscription(db, subscription)
            logger.info('Subscription reactivated via channel event', telegram_id=user.id)

            # Re-enable in RemnaWave panel
            if db_user.remnawave_uuid:
                service = SubscriptionService()
                try:
                    await service.enable_remnawave_user(db_user.remnawave_uuid)
                except Exception as api_error:
                    logger.error('Failed to enable RemnaWave user', error=api_error)

            # Notify the user
            try:
                texts = get_texts(db_user.language or DEFAULT_LANGUAGE)
                notification_text = texts.t(
                    'SUBSCRIPTION_REACTIVATED_CHANNEL_SUBSCRIBE',
                    'Your subscription has been restored! Thank you for subscribing to the channels.',
                )
                await bot.send_message(user.id, notification_text)
            except Exception as notify_error:
                logger.warning('Failed to send notification', telegram_id=user.id, error=notify_error)

            await db.commit()
        except Exception as e:
            logger.error('Error reactivating subscription on channel join', error=e)
            await db.rollback()


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def on_user_left_channel(event: ChatMemberUpdated, bot: Bot) -> None:
    """User unsubscribed from a channel -- update cache and deactivate VPN if applicable."""
    user = event.old_chat_member.user
    channel_id = str(event.chat.id)  # Normalize int to str (DB stores string)

    # FILTER: Only process events for required channels
    if not await _is_required_channel(channel_id):
        return

    await channel_subscription_service.on_user_left(user.id, channel_id)

    if not settings.CHANNEL_IS_REQUIRED_SUB:
        return

    # Skip admins -- never deactivate admin subscriptions
    if settings.is_admin(user.id):
        return

    # Fetch per-channel settings to decide whether to disable
    channel_settings = await channel_subscription_service.get_channel_settings(channel_id)
    if not channel_settings:
        return

    async with AsyncSessionLocal() as db:
        try:
            db_user = await get_user_by_telegram_id(db, user.id)
            if not db_user or not db_user.subscription:
                return

            subscription = db_user.subscription
            if subscription.status != SubscriptionStatus.ACTIVE.value:
                return

            # Per-channel settings: check if this channel requires deactivation
            if not channel_subscription_service.should_disable_subscription(channel_settings, subscription.is_trial):
                return

            await deactivate_subscription(db, subscription)
            logger.info('Subscription deactivated via channel event', telegram_id=user.id)

            # Disable in RemnaWave panel
            if db_user.remnawave_uuid:
                service = SubscriptionService()
                try:
                    await service.disable_remnawave_user(db_user.remnawave_uuid)
                except Exception as api_error:
                    logger.error('Failed to disable RemnaWave user', error=api_error)

            # Notify the user with channel subscription keyboard
            try:
                texts = get_texts(db_user.language or DEFAULT_LANGUAGE)
                unsub_channels = await channel_subscription_service.get_channels_with_status(user.id)
                notification_text = texts.t(
                    'SUBSCRIPTION_DEACTIVATED_CHANNEL_UNSUBSCRIBE',
                    'Your subscription has been paused because you left a required channel.',
                )
                channel_kb = get_channel_sub_keyboard(unsub_channels, language=db_user.language or DEFAULT_LANGUAGE)
                await bot.send_message(user.id, notification_text, reply_markup=channel_kb)
            except Exception as notify_error:
                logger.warning('Failed to send notification', telegram_id=user.id, error=notify_error)

            await db.commit()
        except Exception as e:
            logger.error('Error deactivating subscription on channel leave', error=e)
            await db.rollback()


def register_handlers(dp_router: Router) -> None:
    """Register channel member event handlers on the dispatcher/router."""
    dp_router.include_router(router)
