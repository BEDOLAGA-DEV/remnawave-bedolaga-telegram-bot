"""Deferred retry queue for failed RemnaWave API calls.

When create_remnawave_user() fails during purchase, the subscription exists
in the bot DB but not in the panel. This queue retries the operation
periodically until it succeeds or max retries are exhausted.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import structlog

from app.database.database import AsyncSessionLocal


logger = structlog.get_logger(__name__)


@dataclass
class RetryItem:
    subscription_id: int
    user_id: int
    action: Literal['create', 'update']
    attempts: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: str | None = None


class RemnaWaveRetryQueue:
    def __init__(self, max_retries: int = 5, interval_seconds: int = 120) -> None:
        self._queue: deque[RetryItem] = deque()
        self._max_retries = max_retries
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    def enqueue(
        self,
        subscription_id: int,
        user_id: int,
        action: Literal['create', 'update'] = 'create',
    ) -> None:
        # Deduplicate by subscription_id
        for item in self._queue:
            if item.subscription_id == subscription_id:
                return
        self._queue.append(
            RetryItem(
                subscription_id=subscription_id,
                user_id=user_id,
                action=action,
            )
        )
        logger.info(
            'Enqueued RemnaWave retry',
            subscription_id=subscription_id,
            user_id=user_id,
            action=action,
            queue_size=len(self._queue),
        )

    async def process_pending(self) -> None:
        if not self._queue:
            return

        from app.database.crud.subscription import get_subscription_by_id
        from app.services.subscription_service import SubscriptionService

        batch = list(self._queue)
        self._queue.clear()

        for item in batch:
            item.attempts += 1
            try:
                async with AsyncSessionLocal() as db:
                    sub = await get_subscription_by_id(db, item.subscription_id)
                    if not sub:
                        logger.warning(
                            'Retry: subscription not found, dropping',
                            subscription_id=item.subscription_id,
                        )
                        continue

                    service = SubscriptionService()
                    if not service.is_configured:
                        self._requeue(item, 'RemnaWave not configured')
                        continue

                    if item.action == 'create':
                        await service.create_remnawave_user(db, sub)
                    else:
                        await service.update_remnawave_user(db, sub)

                    logger.info(
                        'Retry succeeded',
                        subscription_id=item.subscription_id,
                        attempts=item.attempts,
                    )

            except Exception as error:
                self._requeue(item, str(error))

    def _requeue(self, item: RetryItem, error: str) -> None:
        item.last_error = error
        if item.attempts < self._max_retries:
            self._queue.append(item)
            logger.warning(
                'Retry failed, re-enqueued',
                subscription_id=item.subscription_id,
                attempts=item.attempts,
                max_retries=self._max_retries,
                error=error,
            )
        else:
            logger.error(
                'Retry exhausted, dropping (MANUAL INTERVENTION NEEDED)',
                subscription_id=item.subscription_id,
                user_id=item.user_id,
                attempts=item.attempts,
                error=error,
            )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self.process_pending()
        except asyncio.CancelledError:
            raise


# Global instance
remnawave_retry_queue = RemnaWaveRetryQueue()
