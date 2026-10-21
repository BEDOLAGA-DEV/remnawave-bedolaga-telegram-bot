"""Gift confirmation races at the actual Redis FSM mutation boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey

from app.handlers.subscription import gift
from tests.services.test_cart_cleanup_redis import carts  # noqa: F401


pytestmark = pytest.mark.asyncio


class MutationProbe:
    """Insert another selection immediately before a real Redis write."""

    def __init__(self, client, replace):
        self.client = client
        self.replace = replace
        self.armed = False
        self.triggered = False

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def before_write(self):
        if self.armed and not self.triggered:
            self.triggered = True
            await self.replace()

    async def set(self, *args, **kwargs):
        await self.before_write()
        return await self.client.set(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        await self.before_write()
        return await self.client.delete(*args, **kwargs)

    async def eval(self, *args, **kwargs):
        await self.before_write()
        return await self.client.eval(*args, **kwargs)


@pytest.mark.parametrize('outcome', ['price', 'disabled', 'restricted', 'tariff', 'period', 'success'])
@pytest.mark.parametrize('change', ['state', 'data', 'both', 'none'])
async def test_new_selection_survives_at_fsm_write_boundary(carts, monkeypatch, outcome, change):
    from aiogram.fsm.storage.redis import RedisStorage

    service, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-fsm-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original_storage = RedisStorage(client, key_builder=builder, state_ttl=90, data_ttl=120)
    original_context = FSMContext(original_storage, key)
    selection = {
        'gift_checkout_id': 'old',
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
        'label': 'Подарок',
    }
    await original_context.set_state('confirming')
    await original_context.set_data(selection)
    await client.pexpire(builder.build(key, 'state'), 30000)
    await client.pexpire(builder.build(key, 'data'), 20000)
    expected_state = 'new_selection' if change in ('state', 'both') else 'confirming'
    expected_data = {'gift_checkout_id': 'new'} if change in ('data', 'both') else selection

    async def replace():
        if change != 'none':
            await original_context.set_state(expected_state)
            await original_context.set_data(expected_data)

    probe = MutationProbe(client, replace)
    storage = RedisStorage(
        probe,
        key_builder=builder,
        state_ttl=90,
        data_ttl=120,
        json_dumps=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True),
    )
    state = FSMContext(storage, key)
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(gift, '_render_confirmation_summary', lambda *args: ('confirm', None))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('result', None))

    async def purchase(**kwargs):
        probe.armed = True
        if outcome == 'price':
            raise gift.GiftPriceChangedError(100, SimpleNamespace(final_price_kopeks=200))
        failures = {
            'disabled': gift.GiftFeatureDisabledError,
            'restricted': gift.GiftPurchaseRestrictedError,
            'tariff': gift.GiftTariffUnavailableError,
            'period': gift.GiftPeriodUnavailableError,
        }
        if outcome in failures:
            raise failures[outcome]
        return SimpleNamespace()

    monkeypatch.setattr(gift, 'purchase_gift_from_balance', purchase)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=AsyncMock())
    try:
        await gift.handle_gift_confirm(callback, SimpleNamespace(id=uid, language='ru'), AsyncMock(), state)
        assert probe.triggered, 'The concurrent selection must actually run at the write boundary'
        if change != 'none':
            assert await original_context.get_state() == expected_state
            assert await original_context.get_data() == expected_data
            if outcome != 'success':
                callback.message.edit_text.assert_not_awaited()
        elif outcome == 'price':
            assert await original_context.get_state() == 'confirming'
            assert await original_context.get_data() == {**selection, 'gift_expected_price_kopeks': 200}
            assert 0 < await client.pttl(builder.build(key, 'state')) <= 30000
            assert 110000 < await client.pttl(builder.build(key, 'data')) <= 120000
        else:
            assert await original_context.get_state() is None
            assert await original_context.get_data() == {}
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('ttl_kind', ['none', 'seconds', 'timedelta'])
async def test_conditional_fsm_update_supports_absent_keys_and_configured_ttl(carts, ttl_kind):
    from datetime import timedelta

    from aiogram.fsm.storage.redis import RedisStorage

    from app.utils.fsm_selection import mutate_fsm_if_current

    _, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-fsm-ttl-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    ttl = {'none': None, 'seconds': 45, 'timedelta': timedelta(seconds=45)}[ttl_kind]
    storage = RedisStorage(client, key_builder=builder, data_ttl=ttl)
    context = FSMContext(storage, key)
    try:
        assert await mutate_fsm_if_current(context, None, {}, updates={'gift_checkout_id': 'new'})
        assert await context.get_data() == {'gift_checkout_id': 'new'}
        assert await context.get_state() is None
        remaining = await client.pttl(builder.build(key, 'data'))
        assert remaining == -1 if ttl is None else 0 < remaining <= 45000
        assert await mutate_fsm_if_current(context, None, {'gift_checkout_id': 'new'}, clear=True)
        assert await client.mget(builder.build(key, 'state'), builder.build(key, 'data')) == [None, None]
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('failed_command', ['mget', 'eval'])
async def test_conditional_fsm_redis_failure_does_not_fall_back_to_unconditional_clear(carts, failed_command):
    from aiogram.fsm.storage.redis import RedisStorage

    from app.utils.fsm_selection import mutate_fsm_if_current

    _, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-fsm-error-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original_context = FSMContext(RedisStorage(client, key_builder=builder), key)
    await original_context.set_state('confirming')
    await original_context.set_data({'gift_checkout_id': 'old'})
    proxy = MutationProbe(client, AsyncMock())
    setattr(proxy, failed_command, AsyncMock(side_effect=TimeoutError('test timeout')))
    context = FSMContext(RedisStorage(proxy, key_builder=builder), key)
    try:
        assert not await mutate_fsm_if_current(context, 'confirming', {'gift_checkout_id': 'old'}, clear=True)
        assert await original_context.get_state() == 'confirming'
        assert await original_context.get_data() == {'gift_checkout_id': 'old'}
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('change', ['state', 'data', 'both', 'none'])
async def test_legacy_checkout_identity_creation_cannot_overwrite_new_selection(carts, monkeypatch, change):
    from aiogram.fsm.storage.redis import RedisStorage

    _, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-fsm-identity-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original = FSMContext(RedisStorage(client, key_builder=builder), key)
    selection = {'gift_tariff_id': 1, 'gift_period_days': 30, 'gift_expected_price_kopeks': 100}
    await original.set_state('confirming')
    await original.set_data(selection)
    expected_state = 'new_selection' if change in ('state', 'both') else 'confirming'
    expected_data = {'gift_checkout_id': 'new'} if change in ('data', 'both') else selection

    async def replace():
        if change != 'none':
            await original.set_state(expected_state)
            await original.set_data(expected_data)

    probe = MutationProbe(client, replace)
    probe.armed = True
    state = FSMContext(RedisStorage(probe, key_builder=builder), key)
    monkeypatch.setattr(gift, 'user_cart_service', SimpleNamespace(get_user_cart=AsyncMock(return_value=None)))
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    purchase = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(gift, 'purchase_gift_from_balance', purchase)
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('result', None))
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=AsyncMock())
    try:
        await gift.handle_gift_confirm(callback, SimpleNamespace(id=uid, language='ru'), AsyncMock(), state)
        assert probe.triggered
        if change != 'none':
            assert await original.get_state() == expected_state
            assert await original.get_data() == expected_data
            purchase.assert_not_awaited()
            callback.message.edit_text.assert_not_awaited()
        else:
            purchase.assert_awaited_once()
            assert len(purchase.await_args.kwargs['idempotency_key']) == 32
            assert await original.get_data() == {}
            assert await original.get_state() is None
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))
