from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import BaseMiddleware, Bot, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.config import settings
from app.database.crud.campaign import get_campaign_by_start_parameter
from app.database.crud.subscription import deactivate_subscription, reactivate_subscription
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal
from app.database.models import SubscriptionStatus, UserStatus
from app.keyboards.inline import get_channel_sub_keyboard
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.channel_subscription_service import channel_subscription_service
from app.services.subscription_service import SubscriptionService
from app.utils.cache import cache
from app.utils.check_reg_process import is_registration_process


logger = structlog.get_logger(__name__)

# Redis key prefix and TTL for pending /start payload backup
REDIS_PAYLOAD_KEY_PREFIX = 'pending_start_payload:'
REDIS_PAYLOAD_TTL = 3600  # 1 hour


async def save_pending_payload_to_redis(telegram_id: int, payload: str) -> bool:
    """Save pending_start_payload to Redis via the shared cache singleton."""
    try:
        key = f'{REDIS_PAYLOAD_KEY_PREFIX}{telegram_id}'
        result = await cache.set(key, payload, expire=REDIS_PAYLOAD_TTL)
        if result:
            logger.info('Saved pending payload to Redis', payload=payload, telegram_id=telegram_id)
        return result
    except Exception as e:
        logger.error('Failed to save payload to Redis', telegram_id=telegram_id, error=e)
        return False


async def get_pending_payload_from_redis(telegram_id: int) -> str | None:
    """Get pending_start_payload from Redis via the shared cache singleton."""
    try:
        key = f'{REDIS_PAYLOAD_KEY_PREFIX}{telegram_id}'
        return await cache.get(key)
    except Exception as e:
        logger.debug('Failed to get payload from Redis', telegram_id=telegram_id, error=e)
        return None


async def delete_pending_payload_from_redis(telegram_id: int) -> None:
    """Delete pending_start_payload from Redis via the shared cache singleton."""
    try:
        key = f'{REDIS_PAYLOAD_KEY_PREFIX}{telegram_id}'
        await cache.delete(key)
    except Exception:
        pass


class ChannelCheckerMiddleware(BaseMiddleware):
    """Middleware for checking required channel subscriptions.

    OPTIMIZED FOR 100k+ USERS:
    - Does NOT call Telegram API directly in the hot path
    - Reads from Redis cache (TTL 600s) -> PostgreSQL -> rate-limited API fallback
    - Updated in real-time via ChatMemberUpdated events
    """

    def __init__(self):
        logger.info('ChannelCheckerMiddleware initialized (multi-channel mode)')

    @staticmethod
    def _any_channel_has_disable_flag(channels: list[dict]) -> bool:
        """Check if any channel in the list has disable-on-leave flags set."""
        return any(ch.get('disable_trial_on_leave', True) or ch.get('disable_paid_on_leave', False) for ch in channels)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Runtime check (supports toggling without restart)
        if not settings.CHANNEL_IS_REQUIRED_SUB:
            return await handler(event, data)

        # Fast-path bypasses
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id
        elif isinstance(event, Update):
            if event.message:
                telegram_id = event.message.from_user.id
            elif event.callback_query:
                telegram_id = event.callback_query.from_user.id

        if telegram_id is None:
            return await handler(event, data)

        # Skip channel check for lightweight UI callbacks (close/delete notifications)
        if isinstance(event, CallbackQuery) and event.data in (
            'webhook:close',
            'ban_notify:delete',
            'noop',
            'current_page',
        ):
            return await handler(event, data)

        if settings.is_admin(telegram_id):
            return await handler(event, data)

        state: FSMContext = data.get('state')
        current_state = await state.get_state() if state else None
        if is_registration_process(event, current_state):
            return await handler(event, data)

        # Ensure service has bot reference for API fallback
        bot: Bot = data['bot']
        if not channel_subscription_service.bot:
            channel_subscription_service.bot = bot

        # Multi-channel check (Redis -> DB -> API)
        all_channels = await channel_subscription_service.get_channels_with_status(telegram_id)
        unsubscribed = [ch for ch in all_channels if not ch.get('is_subscribed', False)]

        if not unsubscribed:
            # All subscribed -- reactivate if needed
            if self._any_channel_has_disable_flag(all_channels):
                await self._reactivate_subscription_on_subscribe(telegram_id, bot)
            return await handler(event, data)

        # User is NOT subscribed to all channels
        if self._any_channel_has_disable_flag(unsubscribed):
            await self._deactivate_subscription_on_unsubscribe(telegram_id, bot, all_channels)

        await self._capture_start_payload(state, event, bot)

        if isinstance(event, CallbackQuery) and event.data == 'sub_channel_check':
            # Rate limit: max 1 check per 5 seconds per user
            rate_key = f'sub_check_rate:{telegram_id}'
            if await cache.exists(rate_key):
                await event.answer()
                return None
            await cache.set(rate_key, 1, expire=5)

            # Re-check via API for immediate feedback (invalidate cache first)
            await channel_subscription_service.invalidate_user_cache(telegram_id)

            all_channels_fresh = await channel_subscription_service.get_channels_with_status(telegram_id)
            unsubscribed_fresh = [ch for ch in all_channels_fresh if not ch.get('is_subscribed', False)]

            if not unsubscribed_fresh:
                # Now subscribed to all channels
                if self._any_channel_has_disable_flag(all_channels_fresh):
                    await self._reactivate_subscription_on_subscribe(telegram_id, bot)
                return await handler(event, data)

            # Still not all subscribed — update keyboard with colored buttons
            # (subscribed = green, unsubscribed = blue) via Bot API 9.4 style
            user_lang = (
                event.from_user.language_code.split('-')[0]
                if event.from_user and event.from_user.language_code
                else DEFAULT_LANGUAGE
            )

            normalized = _normalize_channels(all_channels_fresh)
            texts = get_texts(user_lang)
            channel_sub_kb = get_channel_sub_keyboard(normalized, language=user_lang)
            text = texts.t(
                'CHANNEL_REQUIRED_TEXT',
                '🔒 Для использования бота подпишитесь на новостной канал, '
                'чтобы получать уведомления о новых возможностях и обновлениях бота. Спасибо!',
            )

                if isinstance(event, CallbackQuery) and event.data == 'sub_channel_check':
                    user_language = DEFAULT_LANGUAGE
                    if event.from_user and event.from_user.language_code:
                        user_language = event.from_user.language_code.split('-')[0]
                    texts = get_texts(user_language)
                    await event.answer(
                        texts.t('CHANNEL_SUBSCRIBE_REQUIRED_ALERT', '❌ Вы не подписались на канал!'),
                        show_alert=True,
                    )
                    return None

                return await self._deny_message(event, bot, channel_link, channel_id)
            logger.warning('⚠️ Неожиданный статус пользователя', telegram_id=telegram_id, status=member.status)
            await self._capture_start_payload(state, event, bot)
            return await self._deny_message(event, bot, channel_link, channel_id)

        except TelegramForbiddenError as e:
            logger.error('❌ Бот заблокирован в канале', channel_id=channel_id, error=e)
            await self._capture_start_payload(state, event, bot)
            return await self._deny_message(event, bot, channel_link, channel_id)
        except TelegramBadRequest as e:
            if 'chat not found' in str(e).lower():
                logger.error('❌ Канал не найден', channel_id=channel_id, error=e)
            elif 'user not found' in str(e).lower():
                logger.error('❌ Пользователь не найден', telegram_id=telegram_id, error=e)
            else:
                logger.error('❌ Ошибка запроса к каналу', channel_id=channel_id, error=e)
            await self._capture_start_payload(state, event, bot)
            return await self._deny_message(event, bot, channel_link, channel_id)
        except TelegramNetworkError as e:
            logger.warning('⚠️ Таймаут при проверке подписки на канал', error=e)
            return await handler(event, data)
        except Exception as e:
            logger.error('❌ Неожиданная ошибка при проверке подписки', error=e)
            return await handler(event, data)

    @staticmethod
    async def _deny_message(
        event: TelegramObject,
        bot: Bot,
        channels: list[dict],
    ):
        user = None
        if isinstance(event, (Message, CallbackQuery)):
            user = getattr(event, 'from_user', None)
        elif isinstance(event, Update):
            if event.message and event.message.from_user:
                user = event.message.from_user
            elif event.callback_query and event.callback_query.from_user:
                user = event.callback_query.from_user

        language = DEFAULT_LANGUAGE
        if user and user.language_code:
            language = user.language_code.split('-')[0]

        normalized = _normalize_channels(channels)

        texts = get_texts(language)
        channel_sub_kb = get_channel_sub_keyboard(normalized, language=language)
        text = texts.t(
            'CHANNEL_REQUIRED_TEXT',
            '🔒 Для использования бота подпишитесь на новостной канал, '
            'чтобы получать уведомления о новых возможностях и обновлениях бота. Спасибо!',
        )

        try:
            if isinstance(event, Message):
                return await event.answer(text, reply_markup=channel_sub_kb)
            if isinstance(event, CallbackQuery):
                try:
                    return await event.message.edit_text(text, reply_markup=channel_sub_kb)
                except TelegramBadRequest as e:
                    if 'message is not modified' in str(e).lower():
                        return await event.answer(text, show_alert=True)
                    raise
            elif isinstance(event, Update) and event.message:
                return await bot.send_message(event.message.chat.id, text, reply_markup=channel_sub_kb)
        except Exception as e:
            logger.error('Error sending subscription prompt', error=e)

    # -- _capture_start_payload ------------------------------------------------

    async def _capture_start_payload(
        self,
        state: FSMContext | None,
        event: TelegramObject,
        bot: Bot | None = None,
    ) -> None:
        """Save /start payload to FSM + Redis so it can be restored after subscription.

        This preserves referral codes, deep links, and other start parameters
        when a user is blocked by the channel subscription requirement.
        """
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id if event.from_user else None

        message: Message | None = None
        if isinstance(event, Message):
            message = event
        elif isinstance(event, (CallbackQuery, Update)):
            message = event.message

        if not message or not message.text:
            return

        text = message.text.strip()
        if not text.startswith('/start'):
            return

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1]:
            return

        payload = parts[1]

        # Save to FSM state
        if state:
            state_data = await state.get_data() or {}
            if state_data.get('pending_start_payload') != payload:
                state_data['pending_start_payload'] = payload
                await state.set_data(state_data)
                logger.info('Saved start payload for user (FSM)', payload=payload, telegram_id=telegram_id)
        else:
            logger.warning('_capture_start_payload: state=None for user', telegram_id=telegram_id)

        # Also save to Redis as backup (in case FSM state is lost)
        if telegram_id:
            await save_pending_payload_to_redis(telegram_id, payload)

        if bot and message.from_user and state:
            await self._try_send_campaign_visit_notification(
                bot,
                message.from_user,
                state,
                payload,
            )

    async def _try_send_campaign_visit_notification(
        self,
        bot: Bot,
        telegram_user: types.User,
        state: FSMContext,
        payload: str,
    ) -> None:
        try:
            state_data = await state.get_data() or {}
        except Exception as error:
            logger.error('Failed to get state data for campaign notification', payload=payload, error=error)
            return

        if state_data.get('campaign_notification_sent'):
            return

        async with AsyncSessionLocal() as db:
            try:
                campaign = await get_campaign_by_start_parameter(
                    db,
                    payload,
                    only_active=True,
                )
                if not campaign:
                    return

                user = await get_user_by_telegram_id(db, telegram_user.id)

                notification_service = AdminNotificationService(bot)
                sent = await notification_service.send_campaign_link_visit_notification(
                    db,
                    telegram_user,
                    campaign,
                    user,
                )
                if sent:
                    await state.update_data(campaign_notification_sent=True)
                await db.commit()
            except Exception as error:
                logger.error('Error sending campaign visit notification', payload=payload, error=error)
                await db.rollback()

    # -- _deactivate (multi-channel) -------------------------------------------

    async def _deactivate_subscription_on_unsubscribe(
        self,
        telegram_id: int,
        bot: Bot,
        channels: list[dict],
    ) -> None:
        """Deactivate subscription when user unsubscribes from required channels."""
        async with AsyncSessionLocal() as db:
            try:
                user = await get_user_by_telegram_id(db, telegram_id)
                if not user or not user.subscription:
                    return

                subscription = user.subscription

                if subscription.status != SubscriptionStatus.ACTIVE.value:
                    return

                # Per-channel settings: check if any unsubscribed channel requires deactivation
                unsubscribed = [ch for ch in channels if not ch.get('is_subscribed', False)]
                should_disable = any(
                    channel_subscription_service.should_disable_subscription(ch, subscription.is_trial)
                    for ch in unsubscribed
                )
                if not should_disable:
                    return

                await deactivate_subscription(db, subscription)
                sub_type = 'trial' if subscription.is_trial else 'paid'
                logger.info(
                    'Subscription deactivated after channel unsubscribe',
                    sub_type=sub_type,
                    telegram_id=telegram_id,
                )

                if user.remnawave_uuid:
                    service = SubscriptionService()
                    try:
                        await service.disable_remnawave_user(user.remnawave_uuid)
                    except Exception as api_error:
                        logger.error(
                            'Failed to disable RemnaWave user',
                            remnawave_uuid=user.remnawave_uuid,
                            api_error=api_error,
                        )

                # Notify user about deactivation
                try:
                    normalized = _normalize_channels(channels)
                    texts = get_texts(user.language if user.language else DEFAULT_LANGUAGE)
                    notification_text = texts.t(
                        'SUBSCRIPTION_DEACTIVATED_CHANNEL_UNSUBSCRIBE',
                        '🚫 Ваша подписка приостановлена, так как вы отписались от канала.\n\n'
                        'Подпишитесь на канал снова, чтобы восстановить доступ к VPN.',
                    )
                    channel_kb = get_channel_sub_keyboard(normalized, language=user.language)
                    await bot.send_message(telegram_id, notification_text, reply_markup=channel_kb)
                except Exception as notify_error:
                    logger.error(
                        'Failed to send deactivation notification to user',
                        telegram_id=telegram_id,
                        notify_error=notify_error,
                    )
                await db.commit()
            except Exception as db_error:
                logger.error(
                    'Error deactivating subscription after channel unsubscribe',
                    telegram_id=telegram_id,
                    db_error=db_error,
                )
                await db.rollback()

    # -- _reactivate -----------------------------------------------------------

    async def _reactivate_subscription_on_subscribe(self, telegram_id: int, bot: Bot) -> None:
        """Reactivate subscription after user subscribes to all required channels."""
        async with AsyncSessionLocal() as db:
            try:
                user = await get_user_by_telegram_id(db, telegram_id)
                if not user or not user.subscription:
                    return

                # Do NOT reactivate for blocked users
                if user.status == UserStatus.BLOCKED.value:
                    logger.info('Skipping reactivation for blocked user', telegram_id=telegram_id)
                    return

                subscription = user.subscription

                # Only reactivate DISABLED subscriptions
                if subscription.status != SubscriptionStatus.DISABLED.value:
                    return

                # Check subscription has not expired
                if subscription.end_date and subscription.end_date <= datetime.now(UTC):
                    return

                await reactivate_subscription(db, subscription)
                sub_type = 'trial' if subscription.is_trial else 'paid'
                logger.info(
                    'Subscription reactivated after channel subscribe',
                    sub_type=sub_type,
                    telegram_id=telegram_id,
                )

                # Enable in RemnaWave
                if user.remnawave_uuid:
                    service = SubscriptionService()
                    try:
                        await service.enable_remnawave_user(user.remnawave_uuid)
                    except Exception as api_error:
                        logger.error(
                            'Failed to enable RemnaWave user',
                            remnawave_uuid=user.remnawave_uuid,
                            api_error=api_error,
                        )

                # Notify user about reactivation
                try:
                    texts = get_texts(user.language if user.language else DEFAULT_LANGUAGE)
                    notification_text = texts.t(
                        'SUBSCRIPTION_REACTIVATED_CHANNEL_SUBSCRIBE',
                        '✅ Ваша подписка восстановлена!\n\nСпасибо, что подписались на канал. VPN снова работает.',
                    )
                    await bot.send_message(telegram_id, notification_text)
                except Exception as notify_error:
                    logger.warning(
                        'Failed to send reactivation notification to user',
                        telegram_id=telegram_id,
                        notify_error=notify_error,
                    )
                await db.commit()
            except Exception as db_error:
                logger.error('Error reactivating subscription', telegram_id=telegram_id, db_error=db_error)
                await db.rollback()


def _normalize_channel_link(link: str) -> str:
    """Normalize channel link: convert @username to https://t.me/username."""
    if not link:
        return link
    link = link.strip()
    if link.startswith('@'):
        return f'https://t.me/{link[1:]}'
    return link


def _normalize_channels(channels: list[dict]) -> list[dict]:
    """Normalize channel links in a list of channel dicts (preserves is_subscribed)."""
    normalized = []
    for ch in channels:
        ch_copy = dict(ch)
        link = ch_copy.get('channel_link')
        if link:
            ch_copy['channel_link'] = _normalize_channel_link(link)
        normalized.append(ch_copy)
    return normalized
