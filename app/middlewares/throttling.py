import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject


logger = structlog.get_logger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    """
    Двухуровневый rate-limiter:
    1. Общий троттлинг — 0.5 сек между любыми сообщениями (UX)
    2. /start burst-лимит — макс N вызовов за окно (anti-spam)

    NOTE: Assumes single-process, single-event-loop execution.
    For multi-worker deployments, replace with Redis-based rate limiting.
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

        self._last_cleanup: float = 0.0
        self._cleanup_interval: float = 30.0

    def _maybe_cleanup(self, now: float) -> None:
        """Periodic cleanup of stale entries. Runs at most once per _cleanup_interval."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cleanup_threshold = now - 60
        self.user_buckets = {uid: ts for uid, ts in self.user_buckets.items() if ts > cleanup_threshold}
        self.start_buckets = {
            uid: [ts for ts in tss if now - ts < self.start_window]
            for uid, tss in self.start_buckets.items()
            if any(now - ts < self.start_window for ts in tss)
        }

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id if event.from_user else None

        if not user_id:
            return await handler(event, data)

        now = time.monotonic()

        # Always run cleanup (independent of throttle path)
        self._maybe_cleanup(now)

        # --- /start burst rate-limit ---
        if isinstance(event, Message) and event.text and event.text.split()[0] == '/start':
            timestamps = self.start_buckets.get(user_id, [])
            timestamps = [ts for ts in timestamps if now - ts < self.start_window]

            if len(timestamps) >= self.start_max_calls:
                cooldown = max(1, int(self.start_window - (now - timestamps[0])) + 1)
                logger.warning(
                    'Rate-limit /start burst exceeded',
                    user_id=user_id,
                    call_count=len(timestamps),
                    window_sec=int(self.start_window),
                    max_calls=self.start_max_calls,
                )
                try:
                    await event.answer(f'⏳ Слишком много запросов. Попробуйте через {cooldown} сек.')
                except TelegramAPIError:
                    pass
                self.start_buckets[user_id] = timestamps
                return None

            timestamps.append(now)
            self.start_buckets[user_id] = timestamps

        # --- Общий троттлинг (0.5 сек) ---
        last_call = self.user_buckets.get(user_id, 0)

        if now - last_call < self.rate_limit:
            logger.warning('Throttling user', user_id=user_id)

            # Для сообщений: молчим только если это состояние работы с тикетами; иначе показываем блок
            if isinstance(event, Message):
                try:
                    fsm: FSMContext | None = data.get('state')
                    current = await fsm.get_state() if fsm else None
                except Exception:
                    current = None
                if current:
                    state_str = str(current)
                    is_ticket_state = (
                        ':waiting_for_message' in state_str or ':waiting_for_reply' in state_str
                    ) and ('TicketStates' in state_str or 'AdminTicketStates' in state_str)
                    if is_ticket_state:
                        return None
                try:
                    await event.answer('⏳ Пожалуйста, не отправляйте сообщения так часто!')
                except TelegramAPIError:
                    pass
                return None
            # Для callback допустим краткое уведомление
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer('⏳ Слишком быстро! Подождите немного.', show_alert=True)
                except TelegramAPIError:
                    pass
                return None

        self.user_buckets[user_id] = now

        return await handler(event, data)
