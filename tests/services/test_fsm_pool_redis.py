"""FSM bursts and cancellations must not exhaust the real Redis pool."""

import asyncio
import os
from uuid import uuid4

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey

from tests.fixtures.real_redis import ensure_real_redis


pytestmark = pytest.mark.asyncio


def redis_url():
    url = os.environ.get('TEST_CART_REDIS_URL')
    if not url:
        if os.environ.get('REQUIRE_REDIS_TESTS') == '1':
            pytest.fail('TEST_CART_REDIS_URL required')
        pytest.skip('isolated Redis not configured')
    return url


async def test_fsm_burst_waits_for_real_pool_and_releases_connections(monkeypatch):
    factory = ensure_real_redis(monkeypatch)
    from aiogram.fsm.storage.redis import RedisStorage

    client = factory.create_redis(redis_url(), max_connections=2, timeout=3)
    prefix = f'w6-fsm-{uuid4().hex}'
    storage = RedisStorage(client, key_builder=DefaultKeyBuilder(prefix=prefix))

    async def update(number):
        state = FSMContext(storage, StorageKey(bot_id=1, chat_id=number, user_id=number))
        await state.set_state('waiting_for_amount')
        await state.set_data({'amount': number})
        assert await state.get_state() == 'waiting_for_amount'
        assert await state.get_data() == {'amount': number}
        await state.clear()

    try:
        await client.ping()
        await asyncio.wait_for(asyncio.gather(*(update(number) for number in range(1, 151))), timeout=15)
        assert await client.ping()
        assert not client.connection_pool._in_use_connections
        assert await client.keys(f'{prefix}:*') == []
    finally:
        keys = await client.keys(f'{prefix}:*')
        if keys:
            await client.delete(*keys)
        await storage.close()


async def test_cancelled_pool_waiters_leave_connection_reusable(monkeypatch):
    factory = ensure_real_redis(monkeypatch)
    client = factory.create_redis(redis_url(), max_connections=1, timeout=3)
    pool = client.connection_pool
    held = None
    waiters = []
    try:
        await client.ping()
        held = await pool.get_connection()
        waiters = [asyncio.create_task(client.ping()) for _ in range(100)]
        await asyncio.sleep(0)
        assert all(not waiter.done() for waiter in waiters)
        for waiter in waiters:
            waiter.cancel()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        await pool.release(held)
        held = None
        assert await asyncio.wait_for(client.ping(), timeout=1)
        assert not pool._in_use_connections
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)
        if held is not None:
            await pool.release(held)
        await client.aclose()
