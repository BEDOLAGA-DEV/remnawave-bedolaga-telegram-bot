import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts

logger = structlog.get_logger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    """
    Двухуровневый rate-limiter:
    1. Общий троттлинг — 0.5 сек между любыми сообщениями (UX)
    2. /start burst-лимит — макс N вызовов за окно (anti-spam)
    """

    def __init__(
        self,
        rate_limit: float = 0.5,
        start_max_calls: int = 3,
        start_window: float = 60.0,
    ):
        self.rate_limit = rate_limit
        self.user_buckets: dict[int, float] = {}

        # /start anti-spam: sliding window per user
        self.start_max_calls = start_max_calls
        self.start_window = start_window
        self.start_buckets: dict[int, list[float]] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id

        if not user_id:
            return await handler(event, data)

        language = DEFAULT_LANGUAGE
        if isinstance(event, (Message, CallbackQuery)) and event.from_user and event.from_user.language_code:
            language = event.from_user.language_code.split('-')[0]
        texts = get_texts(language)

        now = time.time()

        # --- /start burst rate-limit ---
        if isinstance(event, Message) and event.text and event.text.startswith('/start'):
            timestamps = self.start_buckets.get(user_id, [])
            # Оставляем только вызовы внутри окна
            timestamps = [ts for ts in timestamps if now - ts < self.start_window]

            if len(timestamps) >= self.start_max_calls:
                cooldown = int(self.start_window - (now - timestamps[0])) + 1
                logger.warning(
                    'Rate-limit /start для : вызовов за s (лимит)',
                    user_id=user_id,
                    timestamps_count=len(timestamps),
                    start_window=int(self.start_window),
                    start_max_calls=self.start_max_calls,
                )
                try:
                    await event.answer(
                        texts.t(
                            'THROTTLING_START_RATE_LIMIT',
                            '⏳ Слишком много запросов. Попробуйте через {cooldown} сек.',
                        ).format(cooldown=cooldown)
                    )
                except Exception:
                    pass
                self.start_buckets[user_id] = timestamps
                return None

            timestamps.append(now)
            self.start_buckets[user_id] = timestamps

        # --- Общий троттлинг (0.5 сек) ---
        last_call = self.user_buckets.get(user_id, 0)

        if now - last_call < self.rate_limit:
            logger.warning('Throttling для пользователя', user_id=user_id)

            # Для сообщений: молчим только если это состояние работы с тикетами; иначе показываем блок
            if isinstance(event, Message):
                try:
                    fsm: FSMContext = data.get('state')  # может отсутствовать
                    current = await fsm.get_state() if fsm else None
                except Exception:
                    current = None
                is_ticket_state = False
                if current:
                    # Молчим только в состояниях работы с тикетами (user/admin): waiting_for_message / waiting_for_reply
                    lowered = str(current)
                    is_ticket_state = (':waiting_for_message' in lowered or ':waiting_for_reply' in lowered) and (
                        'TicketStates' in lowered or 'AdminTicketStates' in lowered
                    )
                if is_ticket_state:
                    return None
                # В остальных случаях — явный блок
                await event.answer(
                    texts.t(
                        'THROTTLING_MESSAGE_RATE_LIMIT',
                        '⏳ Пожалуйста, не отправляйте сообщения так часто!',
                    )
                )
                return None
            # Для callback допустим краткое уведомление
            if isinstance(event, CallbackQuery):
                await event.answer(
                    texts.t(
                        'THROTTLING_CALLBACK_RATE_LIMIT',
                        '⏳ Слишком быстро! Подождите немного.',
                    ),
                    show_alert=True,
                )
                return None

        self.user_buckets[user_id] = now

        # Периодическая очистка старых записей
        cleanup_threshold = now - 60
        self.user_buckets = {
            uid: timestamp for uid, timestamp in self.user_buckets.items() if timestamp > cleanup_threshold
        }
        # Очистка /start бакетов (раз в ~60 сек, лениво)
        if len(self.start_buckets) > 500:
            self.start_buckets = {
                uid: [ts for ts in tss if now - ts < self.start_window]
                for uid, tss in self.start_buckets.items()
                if any(now - ts < self.start_window for ts in tss)
            }

        return await handler(event, data)
