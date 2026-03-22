from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, SQLAlchemyError

from app.database.database import AsyncSessionLocal
from app.database.models import BroadcastHistory, Subscription, SubscriptionStatus, User, UserStatus
from app.handlers.admin.messages import (
    create_broadcast_keyboard,
    get_custom_users,
    get_target_users,
)


if TYPE_CHECKING:
    from app.cabinet.services.email_service import EmailService


logger = structlog.get_logger(__name__)


VALID_MEDIA_TYPES = {'photo', 'video', 'document'}

# =========================================================================
# Telegram rate limits: ~30 msg/sec для бота.
# batch_size=25 + 1 sec delay = ~25 msg/sec с запасом.
# =========================================================================
_TG_BATCH_SIZE = 25
_TG_BATCH_DELAY = 1.0  # секунда между батчами
_TG_MAX_RETRIES = 3  # retry при FloodWait / transient errors

# Прогресс обновляется каждые ~500 сообщений ИЛИ раз в 5 секунд (что наступит раньше)
_PROGRESS_UPDATE_MESSAGES = 500
_PROGRESS_MIN_INTERVAL_SEC = 5.0

# Email broadcast rate limiting: max 8 emails per second
EMAIL_RATE_LIMIT = 8
EMAIL_BATCH_SIZE = 50


@dataclass(slots=True)
class BroadcastMediaConfig:
    type: str
    file_id: str
    caption: str | None = None


@dataclass(slots=True)
class BroadcastConfig:
    target: str
    message_text: str
    selected_buttons: list[str]
    media: BroadcastMediaConfig | None = None
    initiator_name: str | None = None
    custom_buttons: list[dict] | None = None


@dataclass
class EmailBroadcastConfig:
    """Configuration for email broadcast."""

    target: str
    email_subject: str
    email_html_content: str
    initiator_name: str | None = None


@dataclass(slots=True)
class _EmailRecipient:
    """Скалярные данные получателя email (без ORM)."""

    email: str
    user_name: str


@dataclass(slots=True)
class _BroadcastTask:
    task: asyncio.Task
    cancel_event: asyncio.Event


class BroadcastService:
    """Handles broadcast execution triggered from the admin web API."""

    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._tasks: dict[int, _BroadcastTask] = {}
        self._lock = asyncio.Lock()

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    def is_running(self, broadcast_id: int) -> bool:
        task_entry = self._tasks.get(broadcast_id)
        return bool(task_entry and not task_entry.task.done())

    async def start_broadcast(self, broadcast_id: int, config: BroadcastConfig) -> None:
        if self._bot is None:
            logger.error('Невозможно запустить рассылку : бот не инициализирован', broadcast_id=broadcast_id)
            await self._mark_failed(broadcast_id)
            return

        cancel_event = asyncio.Event()

        async with self._lock:
            if broadcast_id in self._tasks and not self._tasks[broadcast_id].task.done():
                logger.warning('Рассылка уже запущена', broadcast_id=broadcast_id)
                return

            task = asyncio.create_task(
                self._run_broadcast(broadcast_id, config, cancel_event),
                name=f'broadcast-{broadcast_id}',
            )
            self._tasks[broadcast_id] = _BroadcastTask(task=task, cancel_event=cancel_event)
            task.add_done_callback(lambda _: self._tasks.pop(broadcast_id, None))

    async def request_stop(self, broadcast_id: int) -> bool:
        async with self._lock:
            task_entry = self._tasks.get(broadcast_id)
            if not task_entry:
                return False

            task_entry.cancel_event.set()
            return True

    async def _run_broadcast(
        self,
        broadcast_id: int,
        config: BroadcastConfig,
        cancel_event: asyncio.Event,
    ) -> None:
        sent_count = 0
        failed_count = 0
        blocked_count = 0

        try:
            if cancel_event.is_set():
                await self._mark_cancelled(broadcast_id, sent_count, failed_count, blocked_count)
                return

            async with AsyncSessionLocal() as session:
                broadcast = await session.get(BroadcastHistory, broadcast_id)
                if not broadcast:
                    logger.error('Запись рассылки не найдена в БД', broadcast_id=broadcast_id)
                    return

                broadcast.status = 'in_progress'
                broadcast.sent_count = 0
                broadcast.failed_count = 0
                broadcast.blocked_count = 0
                await session.commit()

            # _fetch_recipients теперь возвращает list[int] (telegram_id), а не ORM-объекты
            recipient_ids: list[int] = await self._fetch_recipients(config.target)

            async with AsyncSessionLocal() as session:
                broadcast = await session.get(BroadcastHistory, broadcast_id)
                if not broadcast:
                    logger.error('Запись рассылки удалена до запуска', broadcast_id=broadcast_id)
                    return

                broadcast.total_count = len(recipient_ids)
                await session.commit()

            if cancel_event.is_set():
                await self._mark_cancelled(broadcast_id, sent_count, failed_count, blocked_count)
                return

            if not recipient_ids:
                logger.info('Рассылка : получатели не найдены', broadcast_id=broadcast_id)
                await self._mark_finished(broadcast_id, sent_count, failed_count, blocked_count, cancelled=False)
                return

            keyboard = self._build_keyboard(config.selected_buttons, config.custom_buttons)

            logger.info(
                'Рассылка : начинаем отправку получателям (batch delay=s)',
                broadcast_id=broadcast_id,
                recipient_ids_count=len(recipient_ids),
                TG_BATCH_SIZE=_TG_BATCH_SIZE,
                TG_BATCH_DELAY=_TG_BATCH_DELAY,
            )

            sent_count, failed_count, blocked_count, cancelled_during_run = await self._send_batched(
                broadcast_id,
                recipient_ids,
                config,
                keyboard,
                cancel_event,
            )

            if cancelled_during_run:
                logger.info(
                    'Рассылка была отменена во время выполнения, финальный статус уже установлен',
                    broadcast_id=broadcast_id,
                )
                return

            if cancel_event.is_set():
                logger.info(
                    'Запрос на отмену рассылки пришел после завершения отправки, фиксируем итоговый статус',
                    broadcast_id=broadcast_id,
                )

            await self._mark_finished(
                broadcast_id,
                sent_count,
                failed_count,
                blocked_count,
                cancelled=False,
            )

        except asyncio.CancelledError:
            await self._mark_cancelled(broadcast_id, sent_count, failed_count, blocked_count)
            raise
        except Exception as exc:
            logger.exception('Критическая ошибка при выполнении рассылки', broadcast_id=broadcast_id, exc=exc)
            await self._mark_failed(broadcast_id, sent_count, failed_count, blocked_count)

    async def _fetch_recipients(self, target: str) -> list[int]:
        """Загружает получателей и возвращает список telegram_id (скаляры, не ORM-объекты)."""
        async with AsyncSessionLocal() as session:
            if target.startswith('custom_'):
                criteria = target[len('custom_') :]
                users_orm = await get_custom_users(session, criteria)
            else:
                users_orm = await get_target_users(session, target)

            # Извлекаем telegram_id сразу, пока сессия жива.
            # После выхода из блока ORM-объекты станут detached.
            return [u.telegram_id for u in users_orm if u.telegram_id is not None]

    async def _send_batched(
        self,
        broadcast_id: int,
        recipient_ids: list[int],
        config: BroadcastConfig,
        keyboard: InlineKeyboardMarkup | None,
        cancel_event: asyncio.Event,
    ) -> tuple[int, int, int, bool]:
        """
        Единый метод рассылки для любого количества получателей.

        Батчинг по _TG_BATCH_SIZE сообщений с _TG_BATCH_DELAY задержкой.
        Прогресс обновляется каждые _PROGRESS_UPDATE_MESSAGES сообщений.
        Глобальная пауза при FloodWait.

        Returns (sent_count, failed_count, blocked_count, was_cancelled).
        """
        sent_count = 0
        failed_count = 0
        blocked_count = 0
        blocked_telegram_ids: list[int] = []

        # Глобальная пауза при FloodWait — все корутины ждут
        flood_wait_until: float = 0.0
        last_progress_update: float = 0.0
        last_progress_count: int = 0

        async def send_single(telegram_id: int) -> str:
            """Returns 'sent', 'blocked', or 'failed'."""
            nonlocal flood_wait_until

            for attempt in range(_TG_MAX_RETRIES):
                # Глобальная пауза при FloodWait
                now = asyncio.get_event_loop().time()
                if flood_wait_until > now:
                    await asyncio.sleep(flood_wait_until - now)

                if cancel_event.is_set():
                    return 'failed'

                try:
                    await self._deliver_message(telegram_id, config, keyboard)
                    return 'sent'

                except TelegramRetryAfter as e:
                    wait_seconds = e.retry_after + 1
                    flood_wait_until = asyncio.get_event_loop().time() + wait_seconds
                    logger.warning(
                        'FloodWait рассылки : Telegram просит сек (user попытка /)',
                        broadcast_id=broadcast_id,
                        retry_after=e.retry_after,
                        telegram_id=telegram_id,
                        attempt=attempt + 1,
                        TG_MAX_RETRIES=_TG_MAX_RETRIES,
                    )
                    await asyncio.sleep(wait_seconds)

                except TelegramForbiddenError:
                    return 'blocked'

                except TelegramBadRequest as e:
                    err = str(e).lower()
                    if 'bot was blocked' in err or 'user is deactivated' in err or 'chat not found' in err:
                        return 'blocked'
                    return 'failed'

                except Exception as exc:
                    logger.error(
                        'Ошибка отправки рассылки пользователю (попытка /)',
                        broadcast_id=broadcast_id,
                        telegram_id=telegram_id,
                        attempt=attempt + 1,
                        TG_MAX_RETRIES=_TG_MAX_RETRIES,
                        exc=exc,
                    )
                    if attempt < _TG_MAX_RETRIES - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))

            return 'failed'

        for i in range(0, len(recipient_ids), _TG_BATCH_SIZE):
            if cancel_event.is_set():
                await self._mark_cancelled(broadcast_id, sent_count, failed_count, blocked_count)
                return sent_count, failed_count, blocked_count, True

            batch = recipient_ids[i : i + _TG_BATCH_SIZE]
            results = await asyncio.gather(
                *[send_single(tid) for tid in batch],
                return_exceptions=True,
            )

            for idx, result in enumerate(results):
                if isinstance(result, str):
                    if result == 'sent':
                        sent_count += 1
                    elif result == 'blocked':
                        blocked_count += 1
                        blocked_telegram_ids.append(batch[idx])
                    else:
                        failed_count += 1
                elif isinstance(result, Exception):
                    failed_count += 1
                    logger.error('Необработанное исключение в рассылке', broadcast_id=broadcast_id, result=result)

            # Обновляем прогресс в БД периодически
            processed = sent_count + failed_count + blocked_count
            now = asyncio.get_event_loop().time()
            if (
                processed - last_progress_count >= _PROGRESS_UPDATE_MESSAGES
                or now - last_progress_update >= _PROGRESS_MIN_INTERVAL_SEC
            ):
                await self._update_progress(broadcast_id, sent_count, failed_count, blocked_count)
                last_progress_count = processed
                last_progress_update = now

            # Задержка между батчами для rate limiting
            await asyncio.sleep(_TG_BATCH_DELAY)

        return sent_count, failed_count, blocked_count, False

    def _build_keyboard(
        self,
        selected_buttons: list[str] | None,
        custom_buttons: list[dict] | None = None,
    ) -> InlineKeyboardMarkup | None:
        if selected_buttons is None:
            selected_buttons = []
        return create_broadcast_keyboard(selected_buttons, custom_buttons=custom_buttons)

    async def _deliver_message(
        self,
        telegram_id: int,
        config: BroadcastConfig,
        keyboard: InlineKeyboardMarkup | None,
    ) -> None:
        """
        Отправляет одно сообщение.

        НЕ ловит исключения — TelegramRetryAfter, TelegramForbiddenError и др.
        обрабатываются в вызывающем коде (_send_batched).
        """
        if not self._bot:
            raise RuntimeError('Телеграм-бот не инициализирован')

        if config.media and config.media.type in VALID_MEDIA_TYPES:
            caption = config.media.caption or config.message_text
            media_methods = {
                'photo': ('photo', self._bot.send_photo),
                'video': ('video', self._bot.send_video),
                'document': ('document', self._bot.send_document),
            }
            kwarg_name, send_method = media_methods[config.media.type]
            await send_method(
                chat_id=telegram_id,
                **{kwarg_name: config.media.file_id},
                caption=caption,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return

        await self._bot.send_message(
            chat_id=telegram_id,
            text=config.message_text,
            parse_mode='HTML',
            reply_markup=keyboard,
        )

    async def _mark_finished(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
        blocked_count: int = 0,
        *,
        cancelled: bool,
    ) -> None:
        await self._safe_status_update(
            broadcast_id,
            sent_count,
            failed_count,
            blocked_count,
            status='cancelled'
            if cancelled
            else ('completed' if failed_count == 0 and blocked_count == 0 else 'partial'),
        )

    async def _mark_cancelled(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
        blocked_count: int = 0,
    ) -> None:
        await self._mark_finished(
            broadcast_id,
            sent_count,
            failed_count,
            blocked_count,
            cancelled=True,
        )

    async def _mark_failed(
        self,
        broadcast_id: int,
        sent_count: int = 0,
        failed_count: int = 0,
        blocked_count: int = 0,
    ) -> None:
        await self._safe_status_update(
            broadcast_id,
            sent_count,
            failed_count,
            blocked_count,
            status='failed',
        )

    async def _update_progress(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
        blocked_count: int = 0,
    ) -> None:
        """Периодически обновляет прогресс рассылки, чтобы держать соединение активным."""

        await self._safe_status_update(
            broadcast_id,
            sent_count,
            failed_count,
            blocked_count,
            status='in_progress',
            update_completed_at=False,
        )

    async def _safe_status_update(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
        blocked_count: int = 0,
        *,
        status: str,
        update_completed_at: bool = True,
    ) -> None:
        attempts = 0

        while attempts < 2:
            try:
                async with AsyncSessionLocal() as session:
                    broadcast = await session.get(BroadcastHistory, broadcast_id)
                    if not broadcast:
                        return

                    broadcast.sent_count = sent_count
                    broadcast.failed_count = failed_count
                    broadcast.blocked_count = blocked_count
                    broadcast.status = status

                    if update_completed_at:
                        broadcast.completed_at = datetime.now(UTC)

                    await session.commit()
                    return
            except InterfaceError as exc:
                attempts += 1
                logger.warning(
                    'Проблемы с соединением при обновлении статуса рассылки : . Повтор /2',
                    broadcast_id=broadcast_id,
                    exc=exc,
                    attempts=attempts,
                )
                await asyncio.sleep(0.2)
            except SQLAlchemyError:
                logger.exception('Не удалось обновить статус рассылки', broadcast_id=broadcast_id)
                return


async def cleanup_blocked_broadcast_users(blocked_telegram_ids: list[int]) -> None:
    """
    Фоновая очистка пользователей, заблокировавших бота (обнаруженных при рассылке).

    Для каждого telegram_id:
    - Помечает пользователя как BLOCKED
    - Отключает активные подписки (ACTIVE/TRIAL → DISABLED)
    - Отключает пользователя в Remnawave панели
    """
    from app.services.subscription_service import SubscriptionService

    subscription_service = SubscriptionService()

    for telegram_id in blocked_telegram_ids:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User).where(User.telegram_id == telegram_id))
                user = result.scalar_one_or_none()
                if not user or user.status == UserStatus.BLOCKED.value:
                    continue

                user.status = UserStatus.BLOCKED.value

                # Проверяем, есть ли активная оплаченная подписка
                from app.database.crud.subscription import is_active_paid_subscription

                sub_result = await session.execute(select(Subscription).where(Subscription.user_id == user.id))
                user_subscription = sub_result.scalar_one_or_none()

                if is_active_paid_subscription(user_subscription):
                    logger.info(
                        '⏭️ Пропуск отключения подписки: у пользователя активная оплаченная подписка',
                        telegram_id=telegram_id,
                        user_id=user.id,
                    )
                    await session.commit()
                    continue

                # Отключаем активные подписки (только триальные или истёкшие)
                active_sub_result = await session.execute(
                    select(Subscription).where(
                        Subscription.user_id == user.id,
                        Subscription.status.in_(
                            [
                                SubscriptionStatus.ACTIVE.value,
                                SubscriptionStatus.TRIAL.value,
                            ]
                        ),
                    )
                )
                subscriptions = active_sub_result.scalars().all()
                for sub in subscriptions:
                    sub.status = SubscriptionStatus.DISABLED.value

                await session.commit()

                # Отключаем в Remnawave панели (вне транзакции)
                if user.remnawave_uuid:
                    await subscription_service.disable_remnawave_user(user.remnawave_uuid)

                logger.info(
                    'Заблокированный пользователь очищен при рассылке',
                    telegram_id=telegram_id,
                    user_id=user.id,
                    disabled_subs=len(subscriptions),
                )

        except Exception as exc:
            logger.error(
                'Ошибка очистки заблокированного пользователя',
                telegram_id=telegram_id,
                exc=exc,
            )


broadcast_service = BroadcastService()


class EmailBroadcastService:
    """Handles email broadcast execution triggered from the admin web API."""

    def __init__(self) -> None:
        self._email_service: EmailService | None = None
        self._tasks: dict[int, _BroadcastTask] = {}
        self._lock = asyncio.Lock()

    def set_email_service(self, email_service: EmailService) -> None:
        """Set email service instance."""
        self._email_service = email_service

    def is_running(self, broadcast_id: int) -> bool:
        """Check if broadcast is currently running."""
        task_entry = self._tasks.get(broadcast_id)
        return bool(task_entry and not task_entry.task.done())

    async def start_broadcast(self, broadcast_id: int, config: EmailBroadcastConfig) -> None:
        """Start email broadcast in background."""
        if self._email_service is None:
            logger.error('Cannot start email broadcast : email service not initialized', broadcast_id=broadcast_id)
            await self._mark_failed(broadcast_id)
            return

        if not self._email_service.is_configured():
            logger.error('Cannot start email broadcast : SMTP not configured', broadcast_id=broadcast_id)
            await self._mark_failed(broadcast_id)
            return

        cancel_event = asyncio.Event()

        async with self._lock:
            if broadcast_id in self._tasks and not self._tasks[broadcast_id].task.done():
                logger.warning('Email broadcast is already running', broadcast_id=broadcast_id)
                return

            task = asyncio.create_task(
                self._run_broadcast(broadcast_id, config, cancel_event),
                name=f'email-broadcast-{broadcast_id}',
            )
            self._tasks[broadcast_id] = _BroadcastTask(task=task, cancel_event=cancel_event)
            task.add_done_callback(lambda _: self._tasks.pop(broadcast_id, None))

    async def request_stop(self, broadcast_id: int) -> bool:
        """Request to stop a running broadcast."""
        async with self._lock:
            task_entry = self._tasks.get(broadcast_id)
            if not task_entry:
                return False

            task_entry.cancel_event.set()
            return True

    async def _run_broadcast(
        self,
        broadcast_id: int,
        config: EmailBroadcastConfig,
        cancel_event: asyncio.Event,
    ) -> None:
        """Execute email broadcast."""
        sent_count = 0
        failed_count = 0

        try:
            if cancel_event.is_set():
                await self._mark_cancelled(broadcast_id, sent_count, failed_count)
                return

            # Update status to in_progress
            async with AsyncSessionLocal() as session:
                broadcast = await session.get(BroadcastHistory, broadcast_id)
                if not broadcast:
                    logger.error('Broadcast record not found', broadcast_id=broadcast_id)
                    return

                broadcast.status = 'in_progress'
                broadcast.sent_count = 0
                broadcast.failed_count = 0
                await session.commit()

            # Fetch email recipients
            recipients = await self._fetch_email_recipients(config.target)

            # Update total count
            async with AsyncSessionLocal() as session:
                broadcast = await session.get(BroadcastHistory, broadcast_id)
                if not broadcast:
                    logger.error('Broadcast record deleted before start', broadcast_id=broadcast_id)
                    return

                broadcast.total_count = len(recipients)
                await session.commit()

            if cancel_event.is_set():
                await self._mark_cancelled(broadcast_id, sent_count, failed_count)
                return

            if not recipients:
                logger.info('Email broadcast : no recipients found', broadcast_id=broadcast_id)
                await self._mark_finished(broadcast_id, sent_count, failed_count, cancelled=False)
                return

            # Send emails with rate limiting
            sent_count, failed_count, was_cancelled = await self._send_emails(
                broadcast_id,
                recipients,
                config,
                cancel_event,
            )

            if was_cancelled:
                logger.info('Email broadcast was cancelled during execution', broadcast_id=broadcast_id)
                return

            await self._mark_finished(broadcast_id, sent_count, failed_count, cancelled=False)

        except asyncio.CancelledError:
            await self._mark_cancelled(broadcast_id, sent_count, failed_count)
            raise
        except Exception as exc:
            logger.exception('Critical error in email broadcast', broadcast_id=broadcast_id, exc=exc)
            await self._mark_failed(broadcast_id, sent_count, failed_count)

    async def _fetch_email_recipients(self, target: str) -> list[_EmailRecipient]:
        """
        Загружает получателей email-рассылки.

        Возвращает список _EmailRecipient (скалярные данные), а не ORM-объектов,
        чтобы избежать detached state при долгих рассылках.
        """
        from sqlalchemy import select

        from app.database.models import Subscription, SubscriptionStatus, User

        async with AsyncSessionLocal() as session:
            # Base query: verified email users with active status
            base_conditions = [
                User.email.isnot(None),
                User.email_verified == True,
                User.status == 'active',
            ]

            if target == 'all_email':
                query = select(User).where(*base_conditions)

            elif target == 'email_only':
                query = select(User).where(
                    *base_conditions,
                    User.auth_type == 'email',
                )

            elif target == 'telegram_with_email':
                query = select(User).where(
                    *base_conditions,
                    User.auth_type == 'telegram',
                    User.telegram_id.isnot(None),
                )

            elif target == 'active_email':
                query = (
                    select(User)
                    .join(Subscription, User.id == Subscription.user_id)
                    .where(
                        *base_conditions,
                        Subscription.status == SubscriptionStatus.ACTIVE.value,
                    )
                )

            elif target == 'expired_email':
                query = (
                    select(User)
                    .join(Subscription, User.id == Subscription.user_id)
                    .where(
                        *base_conditions,
                        Subscription.status.in_(
                            [
                                SubscriptionStatus.EXPIRED.value,
                                SubscriptionStatus.DISABLED.value,
                            ]
                        ),
                    )
                )

            else:
                logger.warning('Unknown email target filter', target=target)
                return []

            # Загружаем батчами и извлекаем скаляры сразу
            recipients: list[_EmailRecipient] = []
            offset = 0
            batch_size = 1000

            while True:
                result = await session.execute(query.offset(offset).limit(batch_size))
                batch = result.scalars().all()

                if not batch:
                    break

                for user in batch:
                    email = user.email
                    if not email:
                        continue

                    # Формируем имя пользователя
                    user_name = user.username
                    if not user_name:
                        user_name = user.first_name or ''
                        if last_name := user.last_name:
                            user_name = f'{user_name} {last_name}'.strip()
                    if not user_name:
                        user_name = email.split('@')[0]

                    recipients.append(_EmailRecipient(email=email, user_name=user_name))

                offset += batch_size

            return recipients

    async def _send_emails(
        self,
        broadcast_id: int,
        recipients: list[_EmailRecipient],
        config: EmailBroadcastConfig,
        cancel_event: asyncio.Event,
    ) -> tuple[int, int, bool]:
        """
        Отправляет email-рассылку с rate limiting.

        Использует run_in_executor для синхронного SMTP, ограничивая
        параллельность семафором EMAIL_RATE_LIMIT.
        """
        sent_count = 0
        failed_count = 0
        last_progress_count = 0
        last_progress_time: float = 0.0

        semaphore = asyncio.Semaphore(EMAIL_RATE_LIMIT)

        async def send_single_email(recipient: _EmailRecipient) -> bool | None:
            """Отправляет один email."""
            async with semaphore:
                if cancel_event.is_set():
                    return None

                html_content = self._render_template(config.email_html_content, recipient)
                subject = self._render_template(config.email_subject, recipient)

                try:
                    loop = asyncio.get_event_loop()
                    success = await loop.run_in_executor(
                        None,
                        self._email_service.send_email,
                        recipient.email,
                        subject,
                        html_content,
                    )
                    return success
                except Exception as exc:
                    logger.error(
                        'Ошибка отправки email рассылки на', broadcast_id=broadcast_id, email=recipient.email, exc=exc
                    )
                    return False

        for i in range(0, len(recipients), EMAIL_BATCH_SIZE):
            if cancel_event.is_set():
                await self._mark_cancelled(broadcast_id, sent_count, failed_count)
                return sent_count, failed_count, True

            batch = recipients[i : i + EMAIL_BATCH_SIZE]
            tasks = [send_single_email(r) for r in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if result is True:
                    sent_count += 1
                elif result is None:
                    pass  # Cancelled or skipped
                else:
                    failed_count += 1

            # Обновляем прогресс периодически
            processed = sent_count + failed_count
            now = asyncio.get_event_loop().time()
            if (
                processed - last_progress_count >= _PROGRESS_UPDATE_MESSAGES
                or now - last_progress_time >= _PROGRESS_MIN_INTERVAL_SEC
                or i + EMAIL_BATCH_SIZE >= len(recipients)
            ):
                await self._update_progress(broadcast_id, sent_count, failed_count)
                last_progress_count = processed
                last_progress_time = now

            # Rate limiting: ~8 emails/sec
            await asyncio.sleep(EMAIL_BATCH_SIZE / EMAIL_RATE_LIMIT)

        return sent_count, failed_count, False

    @staticmethod
    def _render_template(template: str, recipient: _EmailRecipient) -> str:
        """Подставляет переменные в шаблон email."""
        if not template:
            return template

        result = template.replace('{{user_name}}', recipient.user_name)
        result = result.replace('{{email}}', recipient.email)
        return result

    async def _mark_finished(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
        *,
        cancelled: bool,
    ) -> None:
        """Mark broadcast as finished."""
        status = 'cancelled' if cancelled else ('completed' if failed_count == 0 else 'partial')
        await self._safe_status_update(broadcast_id, sent_count, failed_count, status=status)

    async def _mark_cancelled(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
    ) -> None:
        """Mark broadcast as cancelled."""
        await self._mark_finished(broadcast_id, sent_count, failed_count, cancelled=True)

    async def _mark_failed(
        self,
        broadcast_id: int,
        sent_count: int = 0,
        failed_count: int = 0,
    ) -> None:
        """Mark broadcast as failed."""
        await self._safe_status_update(broadcast_id, sent_count, failed_count, status='failed')

    async def _update_progress(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
    ) -> None:
        """Update broadcast progress."""
        await self._safe_status_update(
            broadcast_id,
            sent_count,
            failed_count,
            status='in_progress',
            update_completed_at=False,
        )

    async def _safe_status_update(
        self,
        broadcast_id: int,
        sent_count: int,
        failed_count: int,
        *,
        status: str,
        update_completed_at: bool = True,
    ) -> None:
        """Safely update broadcast status with retry."""
        attempts = 0

        while attempts < 2:
            try:
                async with AsyncSessionLocal() as session:
                    broadcast = await session.get(BroadcastHistory, broadcast_id)
                    if not broadcast:
                        return

                    broadcast.sent_count = sent_count
                    broadcast.failed_count = failed_count
                    broadcast.status = status

                    if update_completed_at:
                        broadcast.completed_at = datetime.now(UTC)

                    await session.commit()
                    return
            except InterfaceError as exc:
                attempts += 1
                logger.warning(
                    'Connection issue updating email broadcast : . Retry /2',
                    broadcast_id=broadcast_id,
                    exc=exc,
                    attempts=attempts,
                )
                await asyncio.sleep(0.2)
            except SQLAlchemyError:
                logger.exception('Failed to update email broadcast status', broadcast_id=broadcast_id)
                return


email_broadcast_service = EmailBroadcastService()
