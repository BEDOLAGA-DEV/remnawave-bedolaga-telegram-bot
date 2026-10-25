"""A slow reset recovery cannot starve unrelated paid expiry delivery."""

import asyncio
from unittest.mock import AsyncMock

from app.services import paid_traffic_reset_service as resets, remnawave_retry_queue as worker


async def test_reset_recovery_wall_budget_preserves_expiry_queue_progress(monkeypatch):
    cancelled = asyncio.Event()

    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(resets, 'process_pending_daily_resets', blocked)
    monkeypatch.setattr(worker, 'PAID_RESET_RECOVERY_BUDGET_SECONDS', 0.01)
    queue = worker.RemnaWaveRetryQueue()
    queue._recover_pending_expiries = AsyncMock()
    await asyncio.wait_for(queue.process_pending(), 1)
    assert cancelled.is_set()
    queue._recover_pending_expiries.assert_awaited_once()
