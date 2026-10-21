"""Conditional FSM mutations for callbacks that await external work."""

from datetime import timedelta
from typing import Any

import structlog
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.memory import MemoryStorage


logger = structlog.get_logger(__name__)


async def mutate_fsm_if_current(
    context: FSMContext,
    expected_state: str | None,
    expected_data: dict[str, Any],
    *,
    updates: dict[str, Any] | None = None,
    clear: bool = False,
    new_state: str | State | None = None,
) -> bool:
    """Compare state AND data before atomically updating or clearing both.

    Redis uses the storage's own key builder, JSON codec and data TTL. Memory
    storage has no await between comparison and mutation. Unsupported storage
    or a Redis error fails closed; never fall back to unconditional writes.
    """
    if clear == (updates is not None):
        raise ValueError('Choose either data updates or clear')
    if clear and new_state is not None:
        raise ValueError('Cannot set a new state while clearing')
    target_state = new_state.state if isinstance(new_state, State) else new_state
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
            if target_state is not None:
                record.state = target_state
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
                ttl = _redis_ttl(storage.data_ttl)
        state_ttl = _redis_ttl(storage.state_ttl) if target_state is not None else ''
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
            else
                if ARGV[6] == '' then
                    redis.call('DEL', KEYS[2])
                elseif ARGV[7] == '' then
                    redis.call('SET', KEYS[2], ARGV[6])
                else
                    redis.call('SET', KEYS[2], ARGV[6], 'EX', ARGV[7])
                end
                if ARGV[8] == '1' then
                    if ARGV[10] == '' then
                        redis.call('SET', KEYS[1], ARGV[9])
                    else
                        redis.call('SET', KEYS[1], ARGV[9], 'EX', ARGV[10])
                    end
                end
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
                '1' if target_state is not None else '0',
                target_state if target_state is not None else '',
                state_ttl,
            )
        )
    except Exception as error:
        logger.warning('Conditional FSM mutation failed', error_type=type(error).__name__)
        return False


def _redis_ttl(configured_ttl: int | timedelta | None) -> str:
    """Match RedisStorage's EX handling before any conditional writes occur."""
    if configured_ttl is None:
        return ''
    if isinstance(configured_ttl, timedelta):
        configured_ttl = int(configured_ttl.total_seconds())
    # Reject booleans and overflow before Lua writes either of the two keys.
    # This safe integer bound leaves room for Redis's millisecond expiry time.
    if type(configured_ttl) is not int or not 0 < configured_ttl <= 2**53 - 1:
        raise ValueError('FSM TTL must be a positive safe integer')
    return str(configured_ttl)
