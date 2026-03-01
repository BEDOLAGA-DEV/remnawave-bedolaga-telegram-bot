import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.discount_offer import (
    deactivate_expired_offers,
    get_latest_claimed_offer_for_user,
    upsert_discount_offer,
)
from app.database.crud.notification import (
    clear_notification_by_type,
    notification_sent,
    record_notification,
)
from app.database.crud.promo_offer_log import log_promo_offer_action
from app.database.crud.subscription import (
    deactivate_subscription,
    extend_subscription,
    get_expired_subscriptions,
    get_expiring_subscriptions,
    get_subscriptions_for_autopay,
)
from app.database.crud.user import (
    cleanup_expired_promo_offer_discounts,
    delete_user,
    get_inactive_users,
    get_user_by_id,
    subtract_user_balance,
)
from app.database.database import AsyncSessionLocal
from app.database.models import (
    MonitoringLog,
    Subscription,
    SubscriptionStatus,
    Ticket,
    TicketStatus,
    User,
    UserPromoGroup,
    UserStatus,
)
from app.external.remnawave_api import (
    RemnaWaveAPIError,
    RemnaWaveUser,
    TrafficLimitStrategy,
    UserStatus as RemnaWaveUserStatus,
)
from app.localization.texts import get_texts
from app.services.notification_delivery_service import (
    notification_delivery_service,
)
from app.services.notification_settings_service import NotificationSettingsService
from app.services.promo_offer_service import promo_offer_service
from app.services.subscription_service import SubscriptionService
from app.utils.cache import cache
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.pricing_utils import apply_percentage_discount
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)
from app.utils.timezone import format_local_datetime


# Кулдаун между повторными уведомлениями об автоплатеже с недостаточным балансом (6 часов)
AUTOPAY_INSUFFICIENT_BALANCE_COOLDOWN_SECONDS: int = 21600

# Размер батча для проверки подписок на каналы (keyset pagination)
_CHANNEL_CHECK_BATCH_SIZE: int = 100


logger = structlog.get_logger(__name__)


LOGO_PATH = Path(settings.LOGO_FILE)


class MonitoringService:
    def __init__(self, bot=None):
        self.is_running = False
        self.subscription_service = SubscriptionService()
        self.bot = bot
        self._notified_users: set[str] = set()
        self._last_cleanup = datetime.now(UTC)
        self._sla_task = None

    async def _send_message_with_logo(
        self,
        chat_id: int | None,
        text: str,
        reply_markup=None,
        parse_mode: str | None = 'HTML',
        user: User | None = None,
    ):
        """Отправляет сообщение, добавляя логотип при необходимости."""
        if not self.bot:
            raise RuntimeError('Bot instance is not available')

        # Skip email-only users (no telegram_id)
        if not chat_id:
            logger.debug('Пропуск уведомления: chat_id не указан (email-пользователь)')
            return None

        # Skip blocked/deleted users to save Telegram rate limits
        if user and user.status in (UserStatus.BLOCKED.value, UserStatus.DELETED.value):
            logger.debug('Пропуск уведомления: пользователь недоступен', user_id=user.id, status=user.status)
            return None

        if settings.ENABLE_LOGO_MODE and LOGO_PATH.exists() and (text is None or len(text) <= 1000):
            try:
                from app.utils.message_patch import _cache_logo_file_id, get_logo_media

                result = await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=get_logo_media(),
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                _cache_logo_file_id(result)
                return result
            except TelegramBadRequest as exc:
                logger.warning(
                    'Не удалось отправить сообщение с логотипом пользователю : . Отправляем текстовое сообщение.',
                    chat_id=chat_id,
                    exc=exc,
                )

        return await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    @staticmethod
    def _is_unreachable_error(error: TelegramBadRequest) -> bool:
        message = str(error).lower()
        unreachable_markers = (
            'chat not found',
            'user is deactivated',
            'bot was blocked by the user',
            "bot can't initiate conversation",
            "can't initiate conversation",
            'user not found',
            'peer id invalid',
        )
        return any(marker in message for marker in unreachable_markers)

    async def _handle_unreachable_user(self, user: User, error: Exception, context: str) -> bool:
        if isinstance(error, TelegramForbiddenError):
            logger.warning('⚠️ Пользователь недоступен: бот заблокирован', telegram_id=user.telegram_id, context=context)
            return True

        if isinstance(error, TelegramBadRequest) and self._is_unreachable_error(error):
            logger.warning('⚠️ Пользователь недоступен', telegram_id=user.telegram_id, context=context, error=error)
            return True

        return False

    async def start_monitoring(self):
        if self.is_running:
            logger.warning('Мониторинг уже запущен')
            return

        self.is_running = True
        logger.info('🔄 Запуск службы мониторинга')
        # Start dedicated SLA loop with its own interval for timely 5-min checks
        try:
            if not self._sla_task or self._sla_task.done():
                self._sla_task = asyncio.create_task(self._sla_loop())
        except Exception as e:
            logger.error('Не удалось запустить SLA-мониторинг', error=e)

        while self.is_running:
            try:
                await self._monitoring_cycle()
                await asyncio.sleep(settings.MONITORING_INTERVAL * 60)

            except Exception as e:
                logger.error('Ошибка в цикле мониторинга', error=e)
                await asyncio.sleep(60)

    def stop_monitoring(self):
        self.is_running = False
        logger.info('ℹ️ Мониторинг остановлен')
        try:
            if self._sla_task and not self._sla_task.done():
                self._sla_task.cancel()
        except Exception:
            pass

    async def _monitoring_cycle(self):
        async with AsyncSessionLocal() as db:
            try:
                await self._cleanup_notification_cache()

                expired_offers = await deactivate_expired_offers(db)
                if expired_offers:
                    logger.info('🧹 Деактивировано просроченных скидочных предложений', expired_offers=expired_offers)

                expired_active_discounts = await cleanup_expired_promo_offer_discounts(db)
                if expired_active_discounts:
                    logger.info(
                        '🧹 Сброшено активных скидок промо-предложений с истекшим сроком',
                        expired_active_discounts=expired_active_discounts,
                    )

                cleaned_test_access = await promo_offer_service.cleanup_expired_test_access(db)
                if cleaned_test_access:
                    logger.info(
                        '🧹 Отозвано истекших тестовых доступов к сквадам', cleaned_test_access=cleaned_test_access
                    )

                await self._check_expired_subscriptions(db)
                await self._check_expiring_subscriptions(db)
                await self._check_trial_expiring_soon(db)
                await self._check_trial_channel_subscriptions(db)
                await self._check_expired_subscription_followups(db)
                if settings.ENABLE_AUTOPAY:
                    await self._process_autopayments(db)
                await self._cleanup_inactive_users(db)
                await self._sync_with_remnawave(db)

                await self._log_monitoring_event(
                    db,
                    'monitoring_cycle_completed',
                    'Цикл мониторинга успешно завершен',
                    {'timestamp': datetime.now(UTC).isoformat()},
                )
                await db.commit()

            except Exception as e:
                logger.error('Ошибка в цикле мониторинга', error=e)
                try:
                    await self._log_monitoring_event(
                        db,
                        'monitoring_cycle_error',
                        f'Ошибка в цикле мониторинга: {e!s}',
                        {'error': str(e)},
                        is_success=False,
                    )
                except Exception:
                    pass
                await db.rollback()

    async def _cleanup_notification_cache(self):
        current_time = datetime.now(UTC)

        if (current_time - self._last_cleanup).total_seconds() >= 3600:
            old_count = len(self._notified_users)
            self._notified_users.clear()
            self._last_cleanup = current_time
            logger.info('🧹 Очищен кеш уведомлений ( записей)', old_count=old_count)

    async def _check_expired_subscriptions(self, db: AsyncSession):
        try:
            from app.database.crud.subscription import is_recently_updated_by_webhook

            expired_subscriptions = await get_expired_subscriptions(db)

            for subscription in expired_subscriptions:
                if is_recently_updated_by_webhook(subscription):
                    logger.debug(
                        'Пропуск expire подписки : обновлена вебхуком недавно', subscription_id=subscription.id
                    )
                    continue

                from app.database.crud.subscription import expire_subscription

                await expire_subscription(db, subscription)

                user = await get_user_by_id(db, subscription.user_id)
                if user and self.bot:
                    await self._send_subscription_expired_notification(user)

                logger.info(
                    "🔴 Подписка пользователя истекла и статус изменен на 'expired'", user_id=subscription.user_id
                )

            if expired_subscriptions:
                await self._log_monitoring_event(
                    db,
                    'expired_subscriptions_processed',
                    f'Обработано {len(expired_subscriptions)} истёкших подписок',
                    {'count': len(expired_subscriptions)},
                )

        except Exception as e:
            logger.error('Ошибка проверки истёкших подписок', error=e)

    async def update_remnawave_user(self, db: AsyncSession, subscription: Subscription) -> RemnaWaveUser | None:
        try:
            from app.database.crud.subscription import is_recently_updated_by_webhook

            if is_recently_updated_by_webhook(subscription):
                logger.debug(
                    'Пропуск RemnaWave обновления подписки : обновлена вебхуком недавно',
                    subscription_id=subscription.id,
                )
                return None

            user = await get_user_by_id(db, subscription.user_id)
            if not user or not user.remnawave_uuid:
                logger.error('RemnaWave UUID не найден для пользователя', user_id=subscription.user_id)
                return None

            # Обновляем subscription в сессии, чтобы избежать detached instance
            try:
                await db.refresh(subscription)
            except Exception:
                pass

            # Re-check guard after refresh (webhook could have committed between first check and refresh)
            if is_recently_updated_by_webhook(subscription):
                logger.debug(
                    'Пропуск RemnaWave обновления подписки : обновлена вебхуком недавно (после refresh)',
                    subscription_id=subscription.id,
                )
                return None

            current_time = datetime.now(UTC)
            is_active = subscription.status == SubscriptionStatus.ACTIVE.value and subscription.end_date > current_time

            if subscription.status == SubscriptionStatus.ACTIVE.value and subscription.end_date <= current_time:
                subscription.status = SubscriptionStatus.EXPIRED.value
                await db.commit()
                is_active = False
                logger.info("📝 Статус подписки обновлен на 'expired'", subscription_id=subscription.id)

            if not self.subscription_service.is_configured:
                logger.warning(
                    'RemnaWave API не настроен. Пропускаем обновление пользователя', user_id=subscription.user_id
                )
                return None

            async with self.subscription_service.get_api_client() as api:
                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                update_kwargs = dict(
                    uuid=user.remnawave_uuid,
                    status=RemnaWaveUserStatus.ACTIVE if is_active else RemnaWaveUserStatus.EXPIRED,
                    expire_at=subscription.end_date,
                    traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
                    traffic_limit_strategy=TrafficLimitStrategy.MONTH,
                    description=settings.format_remnawave_user_description(
                        full_name=user.full_name, username=user.username, telegram_id=user.telegram_id
                    ),
                    active_internal_squads=subscription.connected_squads,
                )

                if hwid_limit is not None:
                    update_kwargs['hwid_device_limit'] = hwid_limit

                updated_user = await api.update_user(**update_kwargs)

                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()

                status_text = 'активным' if is_active else 'истёкшим'
                logger.info(
                    '✅ Обновлен RemnaWave пользователь со статусом',
                    remnawave_uuid=user.remnawave_uuid,
                    status_text=status_text,
                )
                return updated_user

        except RemnaWaveAPIError as e:
            logger.error('Ошибка обновления RemnaWave пользователя', error=e)
            return None
        except Exception as e:
            logger.error('Ошибка обновления RemnaWave пользователя', error=e)
            return None

    async def _check_expiring_subscriptions(self, db: AsyncSession):
        try:
            warning_days = settings.get_autopay_warning_days()
            all_processed_users = set()

            for days in warning_days:
                expiring_subscriptions = await self._get_expiring_paid_subscriptions(db, days)
                sent_count = 0

                for subscription in expiring_subscriptions:
                    user = await get_user_by_id(db, subscription.user_id)
                    if not user:
                        continue

                    # Use user.id for key to support both Telegram and email users
                    user_key = f'user_{user.id}_today'
                    user_identifier = user.telegram_id or f'email:{user.id}'

                    if (
                        await notification_sent(db, user.id, subscription.id, 'expiring', days)
                        or user_key in all_processed_users
                    ):
                        logger.debug(
                            'Уведомление уже отправлено, пропускаем',
                            user_identifier=user_identifier,
                            days=days,
                        )
                        continue

                    should_send = True
                    for other_days in warning_days:
                        if other_days < days:
                            other_subs = await self._get_expiring_paid_subscriptions(db, other_days)
                            if any(s.user_id == user.id for s in other_subs):
                                should_send = False
                                logger.debug(
                                    '🎯 Пропускаем уведомление на дней для пользователя есть более срочное на дней',
                                    days=days,
                                    user_identifier=user_identifier,
                                    other_days=other_days,
                                )
                                break

                    if not should_send:
                        continue

                    # Handle email-only users via notification delivery service
                    if not user.telegram_id:
                        success = await notification_delivery_service.notify_subscription_expiring(
                            user=user,
                            days_left=days,
                            expires_at=subscription.end_date,
                        )
                        if success:
                            await record_notification(db, user.id, subscription.id, 'expiring', days)
                            all_processed_users.add(user_key)
                            sent_count += 1
                            logger.info(
                                '✅ Email-пользователю отправлено уведомление об истечении подписки через дней',
                                user_id=user.id,
                                days=days,
                            )
                        continue

                    if self.bot:
                        success = await self._send_subscription_expiring_notification(user, subscription, days)
                        if success:
                            await record_notification(db, user.id, subscription.id, 'expiring', days)
                            all_processed_users.add(user_key)
                            sent_count += 1
                            logger.info(
                                '✅ Пользователю отправлено уведомление об истечении подписки через дней',
                                telegram_id=user.telegram_id,
                                days=days,
                            )
                        else:
                            logger.warning(
                                '❌ Не удалось отправить уведомление пользователю', telegram_id=user.telegram_id
                            )

                if sent_count > 0:
                    await self._log_monitoring_event(
                        db,
                        'expiring_notifications_sent',
                        f'Отправлено {sent_count} уведомлений об истечении через {days} дней',
                        {'days': days, 'count': sent_count},
                    )

        except Exception as e:
            logger.error('Ошибка проверки истекающих подписок', error=e)

    async def _check_trial_expiring_soon(self, db: AsyncSession):
        try:
            threshold_time = datetime.now(UTC) + timedelta(hours=2)

            result = await db.execute(
                select(Subscription)
                .join(Subscription.user)
                .options(
                    selectinload(Subscription.user).selectinload(User.promo_group),
                    selectinload(Subscription.user)
                    .selectinload(User.user_promo_groups)
                    .selectinload(UserPromoGroup.promo_group),
                )
                .where(
                    and_(
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.is_trial == True,
                        Subscription.end_date <= threshold_time,
                        Subscription.end_date > datetime.now(UTC),
                        User.status == UserStatus.ACTIVE.value,
                    )
                )
            )
            trial_expiring = result.scalars().all()

            for subscription in trial_expiring:
                user = subscription.user
                if not user:
                    continue

                if await notification_sent(db, user.id, subscription.id, 'trial_2h'):
                    continue

                if self.bot:
                    success = await self._send_trial_ending_notification(user, subscription)
                    if success:
                        await record_notification(db, user.id, subscription.id, 'trial_2h')
                        logger.info(
                            '🎁 Пользователю отправлено уведомление об окончании тестовой подписки через 2 часа',
                            telegram_id=user.telegram_id,
                        )

            if trial_expiring:
                await self._log_monitoring_event(
                    db,
                    'trial_expiring_notifications_sent',
                    f'Отправлено {len(trial_expiring)} уведомлений об окончании тестовых подписок',
                    {'count': len(trial_expiring)},
                )

        except Exception as e:
            logger.error('Ошибка проверки истекающих тестовых подписок', error=e)

    async def _check_trial_channel_subscriptions(self, db: AsyncSession):
        """Background reconciliation of channel subscriptions (rate-limited).

        Processes subscriptions in batches using keyset pagination to avoid
        loading all trial subscriptions into memory at once. Each batch gets
        a fresh DB session to avoid holding a connection pool slot for hours.

        When CHANNEL_REQUIRED_FOR_ALL is True, checks ALL active subscriptions
        (not just trials). Otherwise only checks trial subscriptions.
        """
        from app.database.crud.subscription import is_active_paid_subscription, is_recently_updated_by_webhook

        if not settings.CHANNEL_IS_REQUIRED_SUB:
            return

        if not settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE and not settings.CHANNEL_REQUIRED_FOR_ALL:
            logger.debug('Channel unsubscribe check disabled')
            return

        if not self.bot:
            logger.debug('Skipping channel subscription check - bot unavailable')
            return

        from app.database.crud.required_channel import upsert_user_channel_sub
        from app.services.channel_subscription_service import channel_subscription_service
        from app.utils.cache import ChannelSubCache

        channels = await channel_subscription_service.get_required_channels()
        if not channels:
            return

        # Ensure bot is set on service
        if not channel_subscription_service.bot:
            channel_subscription_service.bot = self.bot

        try:
            now = datetime.now(UTC)
            notifications_allowed = (
                NotificationSettingsService.are_notifications_globally_enabled()
                and NotificationSettingsService.is_trial_channel_unsubscribed_enabled()
            )

            disabled_count = 0
            restored_count = 0
            checked_count = 0
            last_id = 0

            # Build the trial/all filter based on CHANNEL_REQUIRED_FOR_ALL setting
            from sqlalchemy import true as sa_true

            is_trial_filter = sa_true() if settings.CHANNEL_REQUIRED_FOR_ALL else Subscription.is_trial.is_(True)

            while True:
                # Fresh session per batch to avoid long-running connections
                async with AsyncSessionLocal() as batch_db:
                    result = await batch_db.execute(
                        select(Subscription)
                        .join(Subscription.user)
                        .options(
                            selectinload(Subscription.user),
                            selectinload(Subscription.tariff),
                        )
                        .where(
                            and_(
                                Subscription.id > last_id,
                                is_trial_filter,
                                Subscription.end_date > now,
                                Subscription.status.in_(
                                    [
                                        SubscriptionStatus.ACTIVE.value,
                                        SubscriptionStatus.DISABLED.value,
                                    ]
                                ),
                                User.status == UserStatus.ACTIVE.value,
                            )
                        )
                        .order_by(Subscription.id)
                        .limit(_CHANNEL_CHECK_BATCH_SIZE)
                    )

                    subscriptions = result.scalars().all()
                    if not subscriptions:
                        break

                    last_id = subscriptions[-1].id

                    for subscription in subscriptions:
                        user = subscription.user
                        if not user or not user.telegram_id:
                            continue

                        # Existing guard: skip if recently updated by webhook
                        if is_recently_updated_by_webhook(subscription):
                            logger.debug(
                                'Skipping subscription: recently updated by webhook',
                                subscription_id=subscription.id,
                            )
                            continue

                        checked_count += 1

                        # Rate-limited check for ALL channels
                        all_subscribed = True
                        for ch in channels:
                            is_member = await channel_subscription_service._rate_limited_check(
                                user.telegram_id, ch['channel_id']
                            )
                            # Update DB + cache
                            await upsert_user_channel_sub(batch_db, user.telegram_id, ch['channel_id'], is_member)
                            await ChannelSubCache.set_sub_status(user.telegram_id, ch['channel_id'], is_member)

                            if not is_member:
                                all_subscribed = False

                        # DEACTIVATE: was active, now not subscribed to all
                        if subscription.status == SubscriptionStatus.ACTIVE.value and not all_subscribed:
                            # Guard: always skip paid subscriptions (user paid money)
                            if is_active_paid_subscription(subscription):
                                continue

                            subscription = await deactivate_subscription(batch_db, subscription)
                            disabled_count += 1
                            logger.info(
                                'Subscription deactivated (channel unsubscribe)',
                                telegram_id=user.telegram_id,
                                subscription_id=subscription.id,
                                is_trial=subscription.is_trial,
                            )

                            if user.remnawave_uuid:
                                try:
                                    await self.subscription_service.disable_remnawave_user(user.remnawave_uuid)
                                except Exception as api_error:
                                    logger.error(
                                        'Failed to disable RemnaWave user',
                                        remnawave_uuid=user.remnawave_uuid,
                                        api_error=api_error,
                                    )

                            if notifications_allowed:
                                if not await notification_sent(
                                    batch_db,
                                    user.id,
                                    subscription.id,
                                    'trial_channel_unsubscribed',
                                ):
                                    sent = await self._send_trial_channel_unsubscribed_notification(user)
                                    if sent:
                                        await record_notification(
                                            batch_db,
                                            user.id,
                                            subscription.id,
                                            'trial_channel_unsubscribed',
                                        )

                        # REACTIVATE: was disabled, now subscribed to all
                        elif subscription.status == SubscriptionStatus.DISABLED.value and all_subscribed:
                            # Guard: traffic limit exhausted
                            if (
                                subscription.traffic_limit_gb
                                and subscription.traffic_used_gb is not None
                                and subscription.traffic_used_gb >= subscription.traffic_limit_gb
                            ):
                                logger.debug(
                                    'Skipping reactivation: traffic exhausted',
                                    subscription_id=subscription.id,
                                    traffic_used=subscription.traffic_used_gb,
                                    traffic_limit=subscription.traffic_limit_gb,
                                )
                                continue

                            # Guard: disabled by webhook, not by monitoring
                            if (
                                subscription.last_webhook_update_at
                                and subscription.updated_at
                                and subscription.last_webhook_update_at
                                >= subscription.updated_at - timedelta(seconds=10)
                            ):
                                logger.debug(
                                    'Skipping reactivation: disabled by RemnaWave panel',
                                    subscription_id=subscription.id,
                                    last_webhook_at=subscription.last_webhook_update_at,
                                    updated_at=subscription.updated_at,
                                )
                                continue

                            subscription.status = SubscriptionStatus.ACTIVE.value
                            subscription.updated_at = datetime.now(UTC)
                            restored_count += 1

                            logger.info(
                                'Subscription restored (channel resubscribe)',
                                telegram_id=user.telegram_id,
                                subscription_id=subscription.id,
                                is_trial=subscription.is_trial,
                            )

                            try:
                                if user.remnawave_uuid:
                                    await self.subscription_service.update_remnawave_user(batch_db, subscription)
                                else:
                                    await self.subscription_service.create_remnawave_user(batch_db, subscription)
                            except Exception as api_error:
                                logger.error(
                                    'Failed to update RemnaWave user',
                                    telegram_id=user.telegram_id,
                                    api_error=api_error,
                                )

                            await clear_notification_by_type(
                                batch_db,
                                subscription.id,
                                'trial_channel_unsubscribed',
                            )

                    # Commit all changes for this batch
                    await batch_db.commit()

            if disabled_count or restored_count:
                check_scope = 'all' if settings.CHANNEL_REQUIRED_FOR_ALL else 'trial'
                await self._log_monitoring_event(
                    db,
                    'trial_channel_subscription_check',
                    (
                        f'Checked {checked_count} {check_scope} subscriptions: '
                        f'disabled {disabled_count}, restored {restored_count}'
                    ),
                    {
                        'checked': checked_count,
                        'disabled': disabled_count,
                        'restored': restored_count,
                        'scope': check_scope,
                    },
                )

        except Exception as error:
            logger.error('Error checking channel subscriptions', error=error)

    async def _check_expired_subscription_followups(self, db: AsyncSession):
        if not NotificationSettingsService.are_notifications_globally_enabled():
            return
        if not self.bot:
            return

        try:
            now = datetime.now(UTC)

            result = await db.execute(
                select(Subscription)
                .options(
                    selectinload(Subscription.user),
                    selectinload(Subscription.tariff),
                )
                .where(
                    and_(
                        Subscription.is_trial == False,
                        Subscription.end_date <= now,
                    )
                )
            )

            all_subscriptions = result.scalars().all()

            # Исключаем суточные тарифы - для них отдельная логика
            subscriptions = [
                sub for sub in all_subscriptions if not (sub.tariff and getattr(sub.tariff, 'is_daily', False))
            ]

            sent_day1 = 0
            sent_wave2 = 0
            sent_wave3 = 0

            for subscription in subscriptions:
                user = subscription.user
                if not user:
                    continue

                if subscription.end_date is None:
                    continue

                time_since_end = now - subscription.end_date
                if time_since_end.total_seconds() < 0:
                    continue

                days_since = time_since_end.total_seconds() / 86400

                # Day 1 reminder
                if NotificationSettingsService.is_expired_1d_enabled() and 1 <= days_since < 2:
                    if not await notification_sent(db, user.id, subscription.id, 'expired_1d'):
                        success = await self._send_expired_day1_notification(user, subscription)
                        if success:
                            await record_notification(db, user.id, subscription.id, 'expired_1d')
                            sent_day1 += 1

                # Second wave (2-3 days) discount
                if NotificationSettingsService.is_second_wave_enabled() and 2 <= days_since < 4:
                    if not await notification_sent(db, user.id, subscription.id, 'expired_discount_wave2'):
                        percent = NotificationSettingsService.get_second_wave_discount_percent()
                        valid_hours = NotificationSettingsService.get_second_wave_valid_hours()
                        offer = await upsert_discount_offer(
                            db,
                            user_id=user.id,
                            subscription_id=subscription.id,
                            notification_type='expired_discount_wave2',
                            discount_percent=percent,
                            bonus_amount_kopeks=0,
                            valid_hours=valid_hours,
                            effect_type='percent_discount',
                        )
                        success = await self._send_expired_discount_notification(
                            user,
                            subscription,
                            percent,
                            offer.expires_at,
                            offer.id,
                            'second',
                        )
                        if success:
                            await record_notification(db, user.id, subscription.id, 'expired_discount_wave2')
                            sent_wave2 += 1

                # Third wave (N days) discount
                if NotificationSettingsService.is_third_wave_enabled():
                    trigger_days = NotificationSettingsService.get_third_wave_trigger_days()
                    if trigger_days <= days_since < trigger_days + 1:
                        if not await notification_sent(db, user.id, subscription.id, 'expired_discount_wave3'):
                            percent = NotificationSettingsService.get_third_wave_discount_percent()
                            valid_hours = NotificationSettingsService.get_third_wave_valid_hours()
                            offer = await upsert_discount_offer(
                                db,
                                user_id=user.id,
                                subscription_id=subscription.id,
                                notification_type='expired_discount_wave3',
                                discount_percent=percent,
                                bonus_amount_kopeks=0,
                                valid_hours=valid_hours,
                                effect_type='percent_discount',
                            )
                            success = await self._send_expired_discount_notification(
                                user,
                                subscription,
                                percent,
                                offer.expires_at,
                                offer.id,
                                'third',
                                trigger_days=trigger_days,
                            )
                            if success:
                                await record_notification(db, user.id, subscription.id, 'expired_discount_wave3')
                                sent_wave3 += 1

            if sent_day1 or sent_wave2 or sent_wave3:
                await self._log_monitoring_event(
                    db,
                    'expired_followups_sent',
                    (f'Follow-ups: 1д={sent_day1}, скидка 2-3д={sent_wave2}, скидка N={sent_wave3}'),
                    {
                        'day1': sent_day1,
                        'wave2': sent_wave2,
                        'wave3': sent_wave3,
                    },
                )

        except Exception as e:
            logger.error('Ошибка проверки напоминаний об истекшей подписке', error=e)

    async def _get_expiring_paid_subscriptions(self, db: AsyncSession, days_before: int) -> list[Subscription]:
        current_time = datetime.now(UTC)
        threshold_date = current_time + timedelta(days=days_before)

        result = await db.execute(
            select(Subscription)
            .options(
                selectinload(Subscription.user),
                selectinload(Subscription.tariff),
            )
            .where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.is_trial == False,
                    Subscription.end_date > current_time,
                    Subscription.end_date <= threshold_date,
                )
            )
        )

        logger.debug('🔍 Поиск платных подписок, истекающих в ближайшие дней', days_before=days_before)
        logger.debug('📅 Текущее время', current_time=current_time)
        logger.debug('📅 Пороговая дата', threshold_date=threshold_date)

        all_subscriptions = result.scalars().all()

        # Исключаем суточные тарифы - для них отдельная логика списания
        subscriptions = [
            sub for sub in all_subscriptions if not (sub.tariff and getattr(sub.tariff, 'is_daily', False))
        ]

        excluded_count = len(all_subscriptions) - len(subscriptions)
        if excluded_count > 0:
            logger.debug('🔄 Исключено суточных подписок из уведомлений', excluded_count=excluded_count)

        logger.info('📊 Найдено платных подписок для уведомлений', subscriptions_count=len(subscriptions))

        return subscriptions

    @staticmethod
    def _get_user_promo_offer_discount_percent(user: User | None) -> int:
        if not user:
            return 0

        try:
            percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0)
        except (TypeError, ValueError):
            return 0

        expires_at = getattr(user, 'promo_offer_discount_expires_at', None)
        if expires_at and expires_at <= datetime.now(UTC):
            return 0

        return max(0, min(100, percent))

    @staticmethod
    async def _consume_user_promo_offer_discount(db: AsyncSession, user: User) -> None:
        percent = MonitoringService._get_user_promo_offer_discount_percent(user)
        if percent <= 0:
            return

        source = getattr(user, 'promo_offer_discount_source', None)
        log_payload = {
            'offer_id': None,
            'percent': percent,
            'source': source,
            'effect_type': None,
        }

        try:
            offer = await get_latest_claimed_offer_for_user(db, user.id, source)
        except Exception as lookup_error:  # pragma: no cover - defensive logging
            logger.warning(
                'Failed to resolve latest claimed promo offer for user', user_id=user.id, lookup_error=lookup_error
            )
            offer = None

        if offer:
            log_payload['offer_id'] = offer.id
            log_payload['effect_type'] = offer.effect_type
            if not log_payload['percent'] and offer.discount_percent:
                log_payload['percent'] = offer.discount_percent

        user.promo_offer_discount_percent = 0
        user.promo_offer_discount_source = None
        user.promo_offer_discount_expires_at = None
        user.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(user)

        try:
            await log_promo_offer_action(
                db,
                user_id=user.id,
                offer_id=log_payload.get('offer_id'),
                action='consumed',
                source=log_payload.get('source'),
                percent=log_payload.get('percent'),
                effect_type=log_payload.get('effect_type'),
                details={'reason': 'autopay_consumed'},
            )
        except Exception as log_error:  # pragma: no cover - defensive logging
            logger.warning('Failed to record promo offer autopay log for user', user_id=user.id, log_error=log_error)
            try:
                await db.rollback()
            except Exception as rollback_error:  # pragma: no cover - defensive logging
                logger.warning(
                    'Failed to rollback session after promo offer autopay log failure', rollback_error=rollback_error
                )

    async def _process_autopayments(self, db: AsyncSession):
        try:
            current_time = datetime.now(UTC)

            result = await db.execute(
                select(Subscription)
                .options(
                    selectinload(Subscription.user).options(
                        selectinload(User.promo_group),
                        selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
                    ),
                    selectinload(Subscription.tariff),
                )
                .where(
                    and_(
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                        Subscription.autopay_enabled == True,
                        Subscription.is_trial == False,
                    )
                )
            )
            all_autopay_subscriptions = result.scalars().all()

            autopay_subscriptions = []
            for sub in all_autopay_subscriptions:
                # Суточные подписки имеют свой собственный механизм продления
                # (DailySubscriptionService), глобальный autopay на них не распространяется
                if sub.tariff and getattr(sub.tariff, 'is_daily', False):
                    logger.debug(
                        'Пропускаем суточную подписку (тариф) в глобальном autopay', sub_id=sub.id, name=sub.tariff.name
                    )
                    continue

                days_before_expiry = (sub.end_date - current_time).days
                if days_before_expiry <= min(sub.autopay_days_before, 3):
                    autopay_subscriptions.append(sub)

            processed_count = 0
            failed_count = 0

            for subscription in autopay_subscriptions:
                from app.database.crud.subscription import is_recently_updated_by_webhook

                if is_recently_updated_by_webhook(subscription):
                    logger.debug(
                        'Пропуск автоплатежа подписки : обновлена вебхуком недавно', subscription_id=subscription.id
                    )
                    continue

                user = subscription.user
                if not user:
                    continue

                user_identifier = user.telegram_id or f'email:{user.id}'

                # Правильный расчет стоимости продления с учетом всех параметров подписки
                renewal_cost = await self.subscription_service.calculate_renewal_price(subscription, 30, db, user=user)
                promo_discount_percent = self._get_user_promo_offer_discount_percent(user)
                charge_amount = renewal_cost
                promo_discount_value = 0

                if renewal_cost > 0 and promo_discount_percent > 0:
                    charge_amount, promo_discount_value = apply_percentage_discount(
                        renewal_cost,
                        promo_discount_percent,
                    )

                autopay_key = f'autopay_{user.id}_{subscription.id}'
                if autopay_key in self._notified_users:
                    continue

                if user.balance_kopeks >= charge_amount:
                    success = await subtract_user_balance(db, user, charge_amount, 'Автопродление подписки')

                    if success:
                        await extend_subscription(db, subscription, 30)
                        await self.subscription_service.update_remnawave_user(
                            db,
                            subscription,
                            reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                            reset_reason='автопродление подписки',
                        )

                        if promo_discount_value > 0:
                            await self._consume_user_promo_offer_discount(db, user)

                        # Send notification via appropriate channel
                        if user.telegram_id and self.bot:
                            await self._send_autopay_success_notification(user, charge_amount, 30)
                        elif not user.telegram_id:
                            # Email-only user - use notification delivery service
                            await notification_delivery_service.notify_autopay_success(
                                user=user,
                                amount_kopeks=charge_amount,
                                new_expires_at=subscription.end_date,
                            )

                        processed_count += 1
                        self._notified_users.add(autopay_key)
                        logger.info(
                            '💳 Автопродление подписки пользователя успешно (списано , скидка %)',
                            user_identifier=user_identifier,
                            charge_amount=charge_amount,
                            promo_discount_percent=promo_discount_percent,
                        )
                    else:
                        failed_count += 1
                        if user.telegram_id and self.bot:
                            await self._send_autopay_failed_notification(user, user.balance_kopeks, charge_amount)
                        elif not user.telegram_id:
                            await notification_delivery_service.notify_autopay_failed(
                                user=user,
                                reason='Ошибка списания средств',
                            )
                        logger.warning(
                            '💳 Ошибка списания средств для автопродления пользователя', user_identifier=user_identifier
                        )
                else:
                    failed_count += 1

                    # Проверяем кулдаун уведомления через Redis, чтобы не спамить
                    # при каждом срабатывании мониторинга
                    cooldown_key = f'autopay_insufficient_balance_notified:{user.id}'
                    should_notify = True

                    try:
                        if await cache.exists(cooldown_key):
                            should_notify = False
                            logger.debug(
                                '💳 Пропуск уведомления о недостаточном балансе для пользователя — кулдаун активен',
                                user_identifier=user_identifier,
                            )
                    except Exception as redis_err:
                        # Fallback: если Redis недоступен — отправляем уведомление
                        logger.warning(
                            '⚠️ Ошибка проверки кулдауна в Redis для пользователя : . Отправляем уведомление.',
                            user_identifier=user_identifier,
                            redis_err=redis_err,
                        )

                    if should_notify:
                        if user.telegram_id and self.bot:
                            await self._send_autopay_failed_notification(user, user.balance_kopeks, charge_amount)
                        elif not user.telegram_id:
                            await notification_delivery_service.notify_autopay_failed(
                                user=user,
                                reason='Недостаточно средств на балансе',
                            )

                        # Ставим ключ кулдауна после отправки
                        try:
                            await cache.set(
                                cooldown_key,
                                1,
                                expire=AUTOPAY_INSUFFICIENT_BALANCE_COOLDOWN_SECONDS,
                            )
                        except Exception as redis_err:
                            logger.warning(
                                '⚠️ Не удалось установить кулдаун в Redis для пользователя',
                                user_identifier=user_identifier,
                                redis_err=redis_err,
                            )

                    logger.warning(
                        '💳 Недостаточно средств для автопродления у пользователя', user_identifier=user_identifier
                    )

            if processed_count > 0 or failed_count > 0:
                await self._log_monitoring_event(
                    db,
                    'autopayments_processed',
                    f'Автоплатежи: успешно {processed_count}, неудачно {failed_count}',
                    {'processed': processed_count, 'failed': failed_count},
                )

        except Exception as e:
            logger.error('Ошибка обработки автоплатежей', error=e)

    async def _send_subscription_expired_notification(self, user: User) -> bool:
        try:
            message = """
⛔ <b>Подписка истекла</b>

Ваша подписка истекла. Для восстановления доступа продлите подписку.

🔧 Доступ к серверам заблокирован до продления.
"""

            from aiogram.types import InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [build_miniapp_or_callback_button(text='💎 Купить подписку', callback_data='menu_buy')],
                    [build_miniapp_or_callback_button(text='💳 Пополнить баланс', callback_data='balance_topup')],
                ]
            )

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if await self._handle_unreachable_user(user, exc, 'уведомление об истечении подписки'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке уведомления об истечении подписки пользователю',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления об истечении подписки пользователю', telegram_id=user.telegram_id, e=e
            )
            return False

    async def _send_subscription_expiring_notification(self, user: User, subscription: Subscription, days: int) -> bool:
        try:
            from app.utils.formatters import format_days_declension

            texts = get_texts(user.language)
            days_text = format_days_declension(days, user.language)

            if settings.ENABLE_AUTOPAY:
                if subscription.autopay_enabled:
                    autopay_status = '✅ Включен - подписка продлится автоматически'
                    action_text = (
                        f'💰 Убедитесь, что на балансе достаточно средств: {texts.format_price(user.balance_kopeks)}'
                    )
                else:
                    autopay_status = '❌ Отключен - не забудьте продлить вручную!'
                    action_text = '💡 Включите автоплатеж или продлите подписку вручную'
            else:
                autopay_status = '❌ Отключен - не забудьте продлить вручную!'
                action_text = '💡 Продлите подписку вручную'

            message = f"""
⚠️ <b>Подписка истекает через {days_text}!</b>

Ваша платная подписка истекает {format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M')}.

💳 <b>Автоплатеж:</b> {autopay_status}

{action_text}
"""

            from aiogram.types import InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        build_miniapp_or_callback_button(
                            text='⏰ Продлить подписку', callback_data='subscription_extend'
                        )
                    ],
                    [build_miniapp_or_callback_button(text='💳 Пополнить баланс', callback_data='balance_topup')],
                    [build_miniapp_or_callback_button(text='📱 Моя подписка', callback_data='menu_subscription')],
                ]
            )

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if await self._handle_unreachable_user(user, exc, 'уведомление об истекающей подписке'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке уведомления об истечении подписки пользователю',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except TelegramNetworkError as e:
            logger.warning(
                'Таймаут отправки уведомления об истечении подписки пользователю', telegram_id=user.telegram_id, e=e
            )
            return False
        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления об истечении подписки пользователю', telegram_id=user.telegram_id, e=e
            )
            return False

    async def _send_trial_ending_notification(self, user: User, subscription: Subscription) -> bool:
        try:
            get_texts(user.language)

            message = """
🎁 <b>Тестовая подписка скоро закончится!</b>

Ваша тестовая подписка истекает через 2 часа.

💎 <b>Не хотите остаться без VPN?</b>
Переходите на полную подписку!

⚡️ Успейте оформить до окончания тестового периода!
"""

            from aiogram.types import InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [build_miniapp_or_callback_button(text='💎 Купить подписку', callback_data='menu_buy')],
                    [build_miniapp_or_callback_button(text='💰 Пополнить баланс', callback_data='balance_topup')],
                ]
            )

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
                user=user,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if await self._handle_unreachable_user(user, exc, 'уведомление о завершении тестовой подписки'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке уведомления о завершении тестовой подписки пользователю',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except TelegramNetworkError as e:
            logger.warning(
                'Таймаут отправки уведомления об окончании тестовой подписки пользователю',
                telegram_id=user.telegram_id,
                e=e,
            )
            return False
        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления об окончании тестовой подписки пользователю',
                telegram_id=user.telegram_id,
                e=e,
            )
            return False

    async def _send_trial_channel_unsubscribed_notification(self, user: User) -> bool:
        try:
            texts = get_texts(user.language)
            template = texts.get(
                'TRIAL_CHANNEL_UNSUBSCRIBED',
                (
                    '🚫 <b>Доступ приостановлен</b>\n\n'
                    'Мы не нашли вашу подписку на наш канал, поэтому тестовая подписка отключена.\n\n'
                    'Подпишитесь на канал и нажмите «{check_button}», чтобы вернуть доступ.'
                ),
            )

            check_button = texts.t('CHANNEL_CHECK_BUTTON', '✅ Я подписался')
            message = template.format(check_button=check_button)

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            from app.services.channel_subscription_service import channel_subscription_service

            unsubscribed = await channel_subscription_service.get_unsubscribed_channels(user.telegram_id)

            buttons = []
            for ch in unsubscribed:
                link = ch.get('channel_link')
                if link:
                    title = ch.get('title') or texts.t('CHANNEL_SUBSCRIBE_BUTTON', '🔗 Подписаться')
                    buttons.append([InlineKeyboardButton(text=f'🔗 {title}', url=link)])
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=check_button,
                        callback_data='sub_channel_check',
                    )
                ]
            )

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
                user=user,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if await self._handle_unreachable_user(user, exc, 'уведомление об отписке от канала'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке уведомления об отписке от канала пользователю',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except TelegramNetworkError as error:
            logger.warning(
                'Таймаут отправки уведомления об отписке от канала пользователю',
                telegram_id=user.telegram_id,
                error=error,
            )
            return False
        except Exception as error:
            logger.error(
                'Ошибка отправки уведомления об отписке от канала пользователю',
                telegram_id=user.telegram_id,
                error=error,
            )
            return False

    async def _send_expired_day1_notification(self, user: User, subscription: Subscription) -> bool:
        try:
            texts = get_texts(user.language)
            template = texts.get(
                'SUBSCRIPTION_EXPIRED_1D',
                (
                    '⛔ <b>Подписка закончилась</b>\n\n'
                    'Доступ был отключён {end_date}. Продлите подписку, чтобы вернуться в сервис.'
                ),
            )
            message = template.format(
                end_date=format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M'),
                price=settings.format_price(settings.PRICE_30_DAYS),
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                            callback_data='subscription_extend',
                        )
                    ],
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                            callback_data='balance_topup',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'), callback_data='menu_support'
                        )
                    ],
                ]
            )

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if await self._handle_unreachable_user(user, exc, 'напоминание об истекшей подписке'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке напоминания об истекшей подписке пользователю',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except TelegramNetworkError as e:
            logger.warning(
                'Таймаут отправки напоминания об истекшей подписке пользователю', telegram_id=user.telegram_id, e=e
            )
            return False
        except Exception as e:
            logger.error(
                'Ошибка отправки напоминания об истекшей подписке пользователю', telegram_id=user.telegram_id, e=e
            )
            return False

    async def _send_expired_discount_notification(
        self,
        user: User,
        subscription: Subscription,
        percent: int,
        expires_at: datetime,
        offer_id: int,
        wave: str,
        trigger_days: int = None,
    ) -> bool:
        try:
            texts = get_texts(user.language)

            if wave == 'second':
                template = texts.get(
                    'SUBSCRIPTION_EXPIRED_SECOND_WAVE',
                    (
                        '🔥 <b>Скидка {percent}% на продление</b>\n\n'
                        'Активируйте предложение, чтобы получить дополнительную скидку. '
                        'Она суммируется с вашей промогруппой и действует до {expires_at}.'
                    ),
                )
            else:
                template = texts.get(
                    'SUBSCRIPTION_EXPIRED_THIRD_WAVE',
                    (
                        '🎁 <b>Индивидуальная скидка {percent}%</b>\n\n'
                        'Прошло {trigger_days} дней без подписки — возвращайтесь и активируйте дополнительную скидку. '
                        'Она суммируется с промогруппой и действует до {expires_at}.'
                    ),
                )

            message = template.format(
                percent=percent,
                expires_at=format_local_datetime(expires_at, '%d.%m.%Y %H:%M'),
                trigger_days=trigger_days or '',
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        build_miniapp_or_callback_button(
                            text='🎁 Получить скидку', callback_data=f'claim_discount_{offer_id}'
                        )
                    ],
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку'),
                            callback_data='subscription_extend',
                        )
                    ],
                    [
                        build_miniapp_or_callback_button(
                            text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'),
                            callback_data='balance_topup',
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'), callback_data='menu_support'
                        )
                    ],
                ]
            )

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return True

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if await self._handle_unreachable_user(user, exc, 'скидочное уведомление'):
                return True
            logger.error(
                'Ошибка Telegram API при отправке скидочного уведомления пользователю',
                telegram_id=user.telegram_id,
                exc=exc,
            )
            return False
        except TelegramNetworkError as e:
            logger.warning('Таймаут отправки скидочного уведомления пользователю', telegram_id=user.telegram_id, e=e)
            return False
        except Exception as e:
            logger.error('Ошибка отправки скидочного уведомления пользователю', telegram_id=user.telegram_id, e=e)
            return False

    async def _send_autopay_success_notification(self, user: User, amount: int, days: int):
        try:
            texts = get_texts(user.language)
            message = texts.AUTOPAY_SUCCESS.format(days=days, amount=settings.format_price(amount))
            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if not await self._handle_unreachable_user(user, exc, 'уведомление об успешном автоплатеже'):
                logger.error(
                    'Ошибка Telegram API при отправке уведомления об автоплатеже пользователю',
                    telegram_id=user.telegram_id,
                    exc=exc,
                )
        except TelegramNetworkError as e:
            logger.warning(
                'Таймаут отправки уведомления об автоплатеже пользователю', telegram_id=user.telegram_id, e=e
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления об автоплатеже пользователю', telegram_id=user.telegram_id, e=e)

    async def _send_autopay_failed_notification(self, user: User, balance: int, required: int):
        try:
            texts = get_texts(user.language)
            message = texts.AUTOPAY_FAILED.format(
                balance=settings.format_price(balance), required=settings.format_price(required)
            )

            from aiogram.types import InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [build_miniapp_or_callback_button(text='💳 Пополнить баланс', callback_data='balance_topup')],
                    [build_miniapp_or_callback_button(text='📱 Моя подписка', callback_data='menu_subscription')],
                ]
            )

            await self._send_message_with_logo(
                chat_id=user.telegram_id,
                text=message,
                parse_mode='HTML',
                reply_markup=keyboard,
            )

        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            if not await self._handle_unreachable_user(user, exc, 'уведомление о неудачном автоплатеже'):
                logger.error(
                    'Ошибка Telegram API при отправке уведомления о неудачном автоплатеже пользователю',
                    telegram_id=user.telegram_id,
                    exc=exc,
                )
        except TelegramNetworkError as e:
            logger.warning(
                'Таймаут отправки уведомления о неудачном автоплатеже пользователю', telegram_id=user.telegram_id, e=e
            )
        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления о неудачном автоплатеже пользователю', telegram_id=user.telegram_id, e=e
            )

    async def _cleanup_inactive_users(self, db: AsyncSession):
        try:
            now = datetime.now(UTC)
            if now.hour != 3:
                return

            inactive_users = await get_inactive_users(db, settings.INACTIVE_USER_DELETE_MONTHS)
            deleted_count = 0

            for user in inactive_users:
                if not user.subscription or not user.subscription.is_active:
                    success = await delete_user(db, user)
                    if success:
                        deleted_count += 1

            if deleted_count > 0:
                await self._log_monitoring_event(
                    db,
                    'inactive_users_cleanup',
                    f'Удалено {deleted_count} неактивных пользователей',
                    {'deleted_count': deleted_count},
                )
                logger.info('🗑️ Удалено неактивных пользователей', deleted_count=deleted_count)

        except Exception as e:
            logger.error('Ошибка очистки неактивных пользователей', error=e)

    async def _sync_with_remnawave(self, db: AsyncSession):
        try:
            now = datetime.now(UTC)
            if now.minute != 0:
                return

            if not self.subscription_service.is_configured:
                logger.warning('RemnaWave API не настроен. Пропускаем синхронизацию')
                return

            async with self.subscription_service.get_api_client() as api:
                system_stats = await api.get_system_stats()

                await self._log_monitoring_event(
                    db, 'remnawave_sync', 'Синхронизация с RemnaWave завершена', {'stats': system_stats}
                )

        except Exception as e:
            logger.error('Ошибка синхронизации с RemnaWave', error=e)
            await self._log_monitoring_event(
                db,
                'remnawave_sync_error',
                f'Ошибка синхронизации с RemnaWave: {e!s}',
                {'error': str(e)},
                is_success=False,
            )

    async def _check_ticket_sla(self, db: AsyncSession):
        try:
            # Quick guards
            # Allow runtime toggle from SupportSettingsService
            try:
                from app.services.support_settings_service import SupportSettingsService

                sla_enabled_runtime = SupportSettingsService.get_sla_enabled()
            except Exception:
                sla_enabled_runtime = getattr(settings, 'SUPPORT_TICKET_SLA_ENABLED', True)
            if not sla_enabled_runtime:
                return
            if not self.bot:
                return
            if not settings.is_admin_notifications_enabled():
                return

            try:
                from app.services.support_settings_service import SupportSettingsService

                sla_minutes = max(1, int(SupportSettingsService.get_sla_minutes()))
            except Exception:
                sla_minutes = max(1, int(getattr(settings, 'SUPPORT_TICKET_SLA_MINUTES', 5)))
            cooldown_minutes = max(1, int(getattr(settings, 'SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES', 15)))
            now = datetime.now(UTC)
            stale_before = now - timedelta(minutes=sla_minutes)
            cooldown_before = now - timedelta(minutes=cooldown_minutes)

            # Tickets to remind: open, no admin reply yet after user's last message (status OPEN), stale by SLA,
            # and either never reminded or cooldown passed
            result = await db.execute(
                select(Ticket)
                .options(selectinload(Ticket.user))
                .where(
                    and_(
                        Ticket.status == TicketStatus.OPEN.value,
                        Ticket.updated_at <= stale_before,
                        or_(Ticket.last_sla_reminder_at.is_(None), Ticket.last_sla_reminder_at <= cooldown_before),
                    )
                )
            )
            tickets = result.scalars().all()
            if not tickets:
                return

            from app.services.admin_notification_service import AdminNotificationService

            reminders_sent = 0
            service = AdminNotificationService(self.bot)

            for ticket in tickets:
                try:
                    waited_minutes = max(0, int((now - ticket.updated_at).total_seconds() // 60))
                    title = (ticket.title or '').strip()
                    if len(title) > 60:
                        title = title[:57] + '...'

                    # Детали пользователя: имя, Telegram ID и username
                    full_name = ticket.user.full_name if ticket.user else 'Unknown'
                    telegram_id_display = ticket.user.telegram_id if ticket.user else '—'
                    username_display = (ticket.user.username or 'отсутствует') if ticket.user else 'отсутствует'

                    text = (
                        f'⏰ <b>Ожидание ответа на тикет превышено</b>\n\n'
                        f'🆔 <b>ID:</b> <code>{ticket.id}</code>\n'
                        f'👤 <b>Пользователь:</b> {full_name}\n'
                        f'🆔 <b>Telegram ID:</b> <code>{telegram_id_display}</code>\n'
                        f'📱 <b>Username:</b> @{username_display}\n'
                        f'📝 <b>Заголовок:</b> {title or "—"}\n'
                        f'⏱️ <b>Ожидает ответа:</b> {waited_minutes} мин\n'
                    )

                    sent = await service.send_ticket_event_notification(text)
                    if sent:
                        ticket.last_sla_reminder_at = now
                        reminders_sent += 1
                        # commit after each to persist timestamp and avoid duplicate reminders on crash
                        await db.commit()
                except Exception as notify_error:
                    logger.error(
                        'Ошибка отправки SLA-уведомления по тикету', ticket_id=ticket.id, notify_error=notify_error
                    )

            if reminders_sent > 0:
                await self._log_monitoring_event(
                    db,
                    'ticket_sla_reminders_sent',
                    f'Отправлено {reminders_sent} SLA-напоминаний по тикетам',
                    {'count': reminders_sent},
                )
        except Exception as e:
            logger.error('Ошибка проверки SLA тикетов', error=e)

    async def _sla_loop(self):
        try:
            interval_seconds = max(10, int(getattr(settings, 'SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS', 60)))
        except Exception:
            interval_seconds = 60
        while self.is_running:
            try:
                async with AsyncSessionLocal() as db:
                    try:
                        await self._check_ticket_sla(db)
                        await db.commit()
                    except Exception as e:
                        logger.error('Ошибка в SLA-проверке', error=e)
                        await db.rollback()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('Ошибка в SLA-цикле', error=e)
            await asyncio.sleep(interval_seconds)

    async def _log_monitoring_event(
        self, db: AsyncSession, event_type: str, message: str, data: dict[str, Any] = None, is_success: bool = True
    ):
        try:
            log_entry = MonitoringLog(event_type=event_type, message=message, data=data or {}, is_success=is_success)

            db.add(log_entry)
            await db.commit()

        except Exception as e:
            logger.error('Ошибка логирования события мониторинга', error=e)

    async def get_monitoring_status(self, db: AsyncSession) -> dict[str, Any]:
        try:
            from sqlalchemy import desc, select

            recent_events_result = await db.execute(
                select(MonitoringLog).order_by(desc(MonitoringLog.created_at)).limit(10)
            )
            recent_events = recent_events_result.scalars().all()

            yesterday = datetime.now(UTC) - timedelta(days=1)

            events_24h_result = await db.execute(select(MonitoringLog).where(MonitoringLog.created_at >= yesterday))
            events_24h = events_24h_result.scalars().all()

            successful_events = sum(1 for event in events_24h if event.is_success)
            failed_events = sum(1 for event in events_24h if not event.is_success)

            return {
                'is_running': self.is_running,
                'last_update': datetime.now(UTC),
                'recent_events': [
                    {
                        'type': event.event_type,
                        'message': event.message,
                        'success': event.is_success,
                        'created_at': event.created_at,
                    }
                    for event in recent_events
                ],
                'stats_24h': {
                    'total_events': len(events_24h),
                    'successful': successful_events,
                    'failed': failed_events,
                    'success_rate': round(successful_events / len(events_24h) * 100, 1) if events_24h else 0,
                },
            }

        except Exception as e:
            logger.error('Ошибка получения статуса мониторинга', error=e)
            return {
                'is_running': self.is_running,
                'last_update': datetime.now(UTC),
                'recent_events': [],
                'stats_24h': {'total_events': 0, 'successful': 0, 'failed': 0, 'success_rate': 0},
            }

    async def force_check_subscriptions(self, db: AsyncSession) -> dict[str, int]:
        from app.database.crud.subscription import is_recently_updated_by_webhook

        try:
            expired_subscriptions = await get_expired_subscriptions(db)
            expired_count = 0

            for subscription in expired_subscriptions:
                if is_recently_updated_by_webhook(subscription):
                    logger.debug(
                        'Пропуск force-check подписки : обновлена вебхуком недавно', subscription_id=subscription.id
                    )
                    continue
                await deactivate_subscription(db, subscription)
                expired_count += 1

            expiring_subscriptions = await get_expiring_subscriptions(db, 1)
            expiring_count = len(expiring_subscriptions)

            autopay_subscriptions = await get_subscriptions_for_autopay(db)
            autopay_processed = 0

            for subscription in autopay_subscriptions:
                user = await get_user_by_id(db, subscription.user_id)
                if user and user.balance_kopeks >= settings.PRICE_30_DAYS:
                    autopay_processed += 1

            await self._log_monitoring_event(
                db,
                'manual_check_subscriptions',
                f'Принудительная проверка: истекло {expired_count}, истекает {expiring_count}, автоплатежей {autopay_processed}',
                {'expired': expired_count, 'expiring': expiring_count, 'autopay_ready': autopay_processed},
            )

            return {'expired': expired_count, 'expiring': expiring_count, 'autopay_ready': autopay_processed}

        except Exception as e:
            logger.error('Ошибка принудительной проверки подписок', error=e)
            return {'expired': 0, 'expiring': 0, 'autopay_ready': 0}

    async def get_monitoring_logs(
        self, db: AsyncSession, limit: int = 50, event_type: str | None = None, page: int = 1, per_page: int = 20
    ) -> list[dict[str, Any]]:
        try:
            from sqlalchemy import desc, select

            query = select(MonitoringLog).order_by(desc(MonitoringLog.created_at))

            if event_type:
                query = query.where(MonitoringLog.event_type == event_type)

            if page > 1 or per_page != 20:
                offset = (page - 1) * per_page
                query = query.offset(offset).limit(per_page)
            else:
                query = query.limit(limit)

            result = await db.execute(query)
            logs = result.scalars().all()

            return [
                {
                    'id': log.id,
                    'event_type': log.event_type,
                    'message': log.message,
                    'data': log.data,
                    'is_success': log.is_success,
                    'created_at': log.created_at,
                }
                for log in logs
            ]

        except Exception as e:
            logger.error('Ошибка получения логов мониторинга', error=e)
            return []

    async def get_monitoring_logs_count(self, db: AsyncSession, event_type: str | None = None) -> int:
        try:
            from sqlalchemy import func, select

            query = select(func.count(MonitoringLog.id))

            if event_type:
                query = query.where(MonitoringLog.event_type == event_type)

            result = await db.execute(query)
            count = result.scalar()

            return count or 0

        except Exception as e:
            logger.error('Ошибка получения количества логов', error=e)
            return 0

    async def get_monitoring_event_types(self, db: AsyncSession) -> list[str]:
        try:
            from sqlalchemy import select

            result = await db.execute(
                select(MonitoringLog.event_type)
                .where(MonitoringLog.event_type.isnot(None))
                .distinct()
                .order_by(MonitoringLog.event_type)
            )

            return [row[0] for row in result.fetchall() if row[0]]

        except Exception as e:
            logger.error('Ошибка получения списка типов событий мониторинга', error=e)
            return []

    async def cleanup_old_logs(self, db: AsyncSession, days: int = 30) -> int:
        try:
            from sqlalchemy import delete

            if days == 0:
                result = await db.execute(delete(MonitoringLog))
            else:
                cutoff_date = datetime.now(UTC) - timedelta(days=days)
                result = await db.execute(delete(MonitoringLog).where(MonitoringLog.created_at < cutoff_date))

            deleted_count = result.rowcount
            await db.commit()

            if days == 0:
                logger.info('🗑️ Удалены все логи мониторинга ( записей)', deleted_count=deleted_count)
            else:
                logger.info('🗑️ Удалено старых записей логов (старше дней)', deleted_count=deleted_count, days=days)

            return deleted_count

        except Exception as e:
            logger.error('Ошибка очистки логов', error=e)
            await db.rollback()
            return 0


monitoring_service = MonitoringService()
