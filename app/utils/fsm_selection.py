"""Conditional FSM mutations for callbacks that await external work."""

from datetime import timedelta
from typing import Any

import structlog
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


logger = structlog.get_logger(__name__)


async def mutate_fsm_if_current(
    context: FSMContext,
    expected_state: str | None,
    expected_data: dict[str, Any],
    *,
    updates: dict[str, Any] | None = None,
    clear: bool = False,
) -> bool:
    """Compare state AND data before atomically updating data or clearing both.

    Redis uses the storage's own key builder, JSON codec and data TTL. Memory
    storage has no await between comparison and mutation. Unsupported storage
    or a Redis error fails closed; never fall back to unconditional writes.
    """
    if clear == (updates is not None):
        raise ValueError('Choose either data updates or clear')
    storage = context.storage
    if isinstance(storage, MemoryStorage):
        record = storage.storage[context.key]
        if record.state != expected_state or record.data != expected_data:
            return False
        if clear:
            record.state = None
            record.data = {}
        else:
            record.data = {**record.data, **updates}
        return True

    # Lazy import also permits MemoryStorage-only installations/test fixtures.
    try:
        from aiogram.fsm.storage.redis import RedisStorage

        if not isinstance(storage, RedisStorage):
            return False
        state_key = storage.key_builder.build(context.key, 'state')
        data_key = storage.key_builder.build(context.key, 'data')
        raw_state, raw_data = await storage.redis.mget(state_key, data_key)
        current_state = raw_state.decode('utf-8') if isinstance(raw_state, bytes) else raw_state
        encoded_data = raw_data.decode('utf-8') if isinstance(raw_data, bytes) else raw_data
        current_data = storage.json_loads(encoded_data) if encoded_data is not None else {}
        if current_state != expected_state or current_data != expected_data:
            return False

        replacement = ''
        ttl = ''
        if not clear:
            new_data = {**current_data, **updates}
            if new_data:
                replacement = storage.json_dumps(new_data)
                configured_ttl = storage.data_ttl
                if configured_ttl is not None:
                    if isinstance(configured_ttl, timedelta):
                        configured_ttl = int(configured_ttl.total_seconds())
                    if not isinstance(configured_ttl, int) or configured_ttl <= 0:
                        return False
                    ttl = str(configured_ttl)
        script = """
            local function matches(key, present, expected)
                local current = redis.call('GET', key)
                if present == '0' then return not current end
                return current == expected
            end
            if not matches(KEYS[1], ARGV[1], ARGV[2]) or not matches(KEYS[2], ARGV[3], ARGV[4]) then
                return 0
            end
            if ARGV[5] == '1' then
                redis.call('DEL', KEYS[1], KEYS[2])
            elseif ARGV[6] == '' then
                redis.call('DEL', KEYS[2])
            elseif ARGV[7] == '' then
                redis.call('SET', KEYS[2], ARGV[6])
            else
                redis.call('SET', KEYS[2], ARGV[6], 'EX', ARGV[7])
            end
            return 1
        """
        return bool(
            await storage.redis.eval(
                script,
                2,
                state_key,
                data_key,
                '0' if raw_state is None else '1',
                raw_state if raw_state is not None else '',
                '0' if raw_data is None else '1',
                raw_data if raw_data is not None else '',
                '1' if clear else '0',
                replacement,
                ttl,
            )
        )
    except Exception as error:
        logger.warning('Conditional FSM mutation failed', error_type=type(error).__name__)
        return False
