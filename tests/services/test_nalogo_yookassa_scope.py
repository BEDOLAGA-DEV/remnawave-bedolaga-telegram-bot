"""NaloGO receipt context for scoped YooKassa payments."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services import (
    nalogo_queue_service as nalogo_queue_service_module,
    nalogo_service as nalogo_service_module,
)
from app.services.nalogo_queue_service import NalogoQueueService
from app.services.nalogo_service import NaloGoService


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.get_keys: list[str] = []
        self.set_keys: list[str] = []
        self.setnx_keys: list[str] = []
        self.delete_keys: list[str] = []
        self.lpush_items: list[tuple[str, Any]] = []

    async def get(self, key: str) -> Any:
        self.get_keys.append(key)
        return self.values.get(key)

    async def set(self, key: str, value: Any, expire: int | None = None) -> bool:
        self.set_keys.append(key)
        self.values[key] = value
        return True

    async def setnx(self, key: str, value: Any, expire: int | None = None) -> bool:
        self.setnx_keys.append(key)
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def lpush(self, key: str, value: Any) -> bool:
        self.lpush_items.append((key, value))
        return True

    async def llen(self, key: str) -> int:
        return sum(1 for item_key, _ in self.lpush_items if item_key == key)

    async def delete(self, key: str) -> bool:
        self.delete_keys.append(key)
        self.values.pop(key, None)
        return True


@pytest.mark.anyio('asyncio')
async def test_nalogo_create_receipt_uses_scoped_yookassa_dedup_keys_when_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cache = FakeCache()
    monkeypatch.setattr(nalogo_service_module, 'cache', fake_cache)

    service = NaloGoService.__new__(NaloGoService)
    service.configured = True
    service.client = SimpleNamespace()

    async def fake_authenticate() -> bool:
        return False

    service.authenticate = fake_authenticate

    result = await service.create_receipt(
        name='Пополнение баланса',
        amount=100.0,
        quantity=1,
        payment_id='yk_cabinet_receipt',
        payment_provider='yookassa',
        payment_scope='cabinet',
        external_payment_id='yk_cabinet_receipt',
    )

    assert result is None
    created_key = 'nalogo:created:yookassa:cabinet:yk_cabinet_receipt'
    assert fake_cache.get_keys == [created_key, created_key]
    assert fake_cache.setnx_keys == ['nalogo:queued:yookassa:cabinet:yk_cabinet_receipt']
    assert fake_cache.lpush_items[0][0] == nalogo_service_module.NALOGO_QUEUE_KEY
    queued_receipt = fake_cache.lpush_items[0][1]
    assert queued_receipt['payment_provider'] == 'yookassa'
    assert queued_receipt['payment_scope'] == 'cabinet'
    assert queued_receipt['external_payment_id'] == 'yk_cabinet_receipt'


@pytest.mark.anyio('asyncio')
async def test_nalogo_queue_replays_scoped_receipt_context(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cache = FakeCache()
    monkeypatch.setattr(nalogo_queue_service_module, 'cache', fake_cache)
    monkeypatch.setattr(settings, 'NALOGO_QUEUE_RECEIPT_DELAY', 0, raising=False)

    create_kwargs: dict[str, Any] = {}

    class FakeNaloGoService:
        configured = True

        def __init__(self) -> None:
            self.queue = [
                {
                    'name': 'Пополнение баланса',
                    'amount': 100.0,
                    'quantity': 1,
                    'payment_id': 'yk_bot_receipt',
                    'payment_provider': 'yookassa',
                    'payment_scope': 'bot',
                    'external_payment_id': 'yk_bot_receipt',
                    'attempts': 0,
                }
            ]

        async def get_queue_length(self) -> int:
            return len(self.queue)

        async def pop_receipt_from_queue(self) -> dict[str, Any] | None:
            return self.queue.pop(0) if self.queue else None

        async def create_receipt(self, **kwargs: Any) -> str:
            create_kwargs.update(kwargs)
            return 'receipt-uuid'

        async def requeue_receipt(self, receipt_data: dict[str, Any]) -> bool:
            self.queue.append(receipt_data)
            return True

    service = NalogoQueueService(FakeNaloGoService())

    await service._process_pending_receipts()

    assert create_kwargs['payment_provider'] == 'yookassa'
    assert create_kwargs['payment_scope'] == 'bot'
    assert create_kwargs['external_payment_id'] == 'yk_bot_receipt'
    assert fake_cache.delete_keys == ['nalogo:queued:yookassa:bot:yk_bot_receipt']
