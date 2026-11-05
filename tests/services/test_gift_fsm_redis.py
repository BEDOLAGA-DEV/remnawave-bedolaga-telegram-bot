"""Gift confirmation races at the actual Redis FSM mutation boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder, StorageKey

from app.handlers.subscription import gift
from tests.fixtures.gift_callbacks import bind_gift_action
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
        await bind_gift_action(callback, state, 'gift_confirm')
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
        await bind_gift_action(callback, state, 'gift_confirm')
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


@pytest.mark.parametrize('outcome', ['enough', 'shortfall', 'disabled', 'restricted', 'tariff', 'period', 'paid'])
@pytest.mark.parametrize('change', ['state', 'data', 'both', 'none'])
async def test_gift_resume_cannot_overwrite_selection_at_fsm_write(carts, monkeypatch, outcome, change):
    from unittest.mock import MagicMock

    from aiogram.fsm.storage.redis import RedisStorage

    service, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-fsm-resume-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original = FSMContext(RedisStorage(client, key_builder=builder), key)
    selection = {'gift_checkout_id': 'old', 'other': 'keep'}
    await original.set_state('old_selection')
    await original.set_data(selection)
    expected_state = 'new_selection' if change in ('state', 'both') else 'old_selection'
    expected_data = {'gift_checkout_id': 'new'} if change in ('data', 'both') else selection
    await service.save_user_cart(
        uid,
        {
            'cart_mode': 'gift_purchase',
            'gift_checkout_id': 'old',
            'user_id': uid,
            'tariff_id': 1,
            'period_days': 30,
            'total_price': 100,
            'return_to_cart': True,
        },
    )

    async def replace():
        if change != 'none':
            await original.set_state(expected_state)
            await original.set_data(expected_data)

    probe = MutationProbe(client, replace)
    state = FSMContext(RedisStorage(probe, key_builder=builder, state_ttl=90, data_ttl=120), key)
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, '_render_confirmation_summary', lambda *args: ('confirm', None))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('result', None))
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    failures = {
        'disabled': gift.GiftFeatureDisabledError,
        'restricted': gift.GiftPurchaseRestrictedError,
        'tariff': gift.GiftTariffUnavailableError,
        'period': gift.GiftPeriodUnavailableError,
    }

    async def quote(*args, **kwargs):
        probe.armed = True
        if outcome in failures:
            raise failures[outcome]
        return SimpleNamespace(final_price_kopeks=100)

    async def purchase(**kwargs):
        probe.armed = True
        return SimpleNamespace()

    monkeypatch.setattr(gift, 'quote_gift_purchase', quote)
    monkeypatch.setattr(gift, 'purchase_gift_from_balance', purchase)
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = (
        SimpleNamespace(status=gift.GuestPurchaseStatus.PAID, tariff_id=1, period_days=30, amount_kopeks=100)
        if outcome == 'paid'
        else None
    )
    db.execute.return_value = result
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=AsyncMock())
    try:
        await gift.handle_return_to_gift_cart(
            callback,
            SimpleNamespace(id=uid, language='ru', balance_kopeks=50 if outcome == 'shortfall' else 200),
            db,
            state,
        )
        assert probe.triggered
        if change != 'none':
            assert await original.get_state() == expected_state
            assert await original.get_data() == expected_data
            if outcome != 'paid':
                callback.message.edit_text.assert_not_awaited()
        elif outcome in ('enough', 'shortfall'):
            assert await original.get_state() == gift.GiftPurchaseStates.confirming_purchase.state
            assert await original.get_data() == {
                **selection,
                'gift_checkout_selection': [1, 30],
                'gift_tariff_id': 1,
                'gift_period_days': 30,
                'gift_expected_price_kopeks': 100,
            }
            assert 80000 < await client.pttl(builder.build(key, 'state')) <= 90000
            assert 110000 < await client.pttl(builder.build(key, 'data')) <= 120000
        else:
            assert await original.get_state() is None
            assert await original.get_data() == {}
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('ttl_kind', ['none', 'seconds', 'timedelta'])
async def test_conditional_fsm_transition_sets_both_ttls_and_preserves_other_fields(carts, ttl_kind):
    from datetime import timedelta

    from aiogram.fsm.storage.redis import RedisStorage

    from app.utils.fsm_selection import mutate_fsm_if_current

    _, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-fsm-transition-ttl', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    state_ttl = {'none': None, 'seconds': 45, 'timedelta': timedelta(seconds=45)}[ttl_kind]
    data_ttl = {'none': None, 'seconds': 60, 'timedelta': timedelta(seconds=60)}[ttl_kind]
    context = FSMContext(RedisStorage(client, key_builder=builder, state_ttl=state_ttl, data_ttl=data_ttl), key)
    try:
        await context.set_state('old')
        await context.set_data({'other': 'keep'})
        assert await mutate_fsm_if_current(
            context,
            'old',
            {'other': 'keep'},
            new_state=gift.GiftPurchaseStates.confirming_purchase,
            updates={'gift_checkout_id': 'restored'},
        )
        assert await context.get_state() == gift.GiftPurchaseStates.confirming_purchase.state
        assert await context.get_data() == {'other': 'keep', 'gift_checkout_id': 'restored'}
        for field, limit in [('state', 45000), ('data', 60000)]:
            remaining = await client.pttl(builder.build(key, field))
            assert remaining == -1 if ttl_kind == 'none' else 0 < remaining <= limit
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('field,invalid_ttl', [('state', 2**63), ('state', True), ('data', 0), ('data', -1)])
async def test_invalid_fsm_transition_ttl_never_partially_changes_selection(carts, field, invalid_ttl):
    from aiogram.fsm.storage.redis import RedisStorage

    from app.utils.fsm_selection import mutate_fsm_if_current

    _, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-fsm-invalid-ttl', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original = FSMContext(RedisStorage(client, key_builder=builder), key)
    await original.set_state('old')
    await original.set_data({'gift_checkout_id': 'old'})
    storage = RedisStorage(client, key_builder=builder, **{f'{field}_ttl': invalid_ttl})
    try:
        assert not await mutate_fsm_if_current(
            FSMContext(storage, key),
            'old',
            {'gift_checkout_id': 'old'},
            new_state='new',
            updates={'gift_checkout_id': 'new'},
        )
        assert await original.get_state() == 'old'
        assert await original.get_data() == {'gift_checkout_id': 'old'}
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('change', ['state', 'data', 'both', 'cart_only', 'none'])
async def test_bound_cancel_preserves_changes_at_atomic_fsm_write(carts, monkeypatch, change):
    from aiogram.fsm.storage.redis import RedisStorage

    service, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-bound-cancel-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original = FSMContext(RedisStorage(client, key_builder=builder), key)
    selection = {
        'gift_checkout_id': 'old',
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
    }
    await original.set_state('confirming')
    await original.set_data(selection)
    await service.save_user_cart(uid, {'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
    await service.save_user_cart(uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'old', 'return_to_cart': True})
    old_cart = await service.get_user_cart(uid)
    replacement = None
    expected_state = 'new_selection' if change in ('state', 'both') else 'confirming'
    expected_data = {'gift_checkout_id': 'new'} if change in ('data', 'both') else selection

    async def replace():
        nonlocal replacement
        if change in ('state', 'data', 'both'):
            await original.set_state(expected_state)
            await original.set_data(expected_data)
        if change == 'cart_only':
            await service.save_user_cart(
                uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'new', 'return_to_cart': True}
            )
            replacement = await service.get_user_cart(uid)

    probe = MutationProbe(client, replace)
    probe.armed = True
    state = FSMContext(RedisStorage(probe, key_builder=builder), key)
    monkeypatch.setattr(gift, 'user_cart_service', service)
    show_origin = AsyncMock()
    monkeypatch.setattr(gift, 'show_subscription_info', show_origin)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await bind_gift_action(callback, state, 'gift_cancel')
    try:
        await gift.handle_gift_cancel(callback, SimpleNamespace(id=uid, language='ru'), AsyncMock(), state)
        assert probe.triggered
        assert await service.has_topup_intent(uid, independent)
        if change in ('state', 'data', 'both'):
            assert await original.get_state() == expected_state
            assert await original.get_data() == expected_data
            assert await service.get_user_cart(uid) == old_cart
            show_origin.assert_not_awaited()
        else:
            assert await original.get_state() is None
            assert await original.get_data() == {}
            assert await service.get_user_cart(uid) == replacement
            show_origin.assert_awaited_once()
            if replacement:
                assert await service.has_topup_intent(uid, replacement)
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize(
    'selection,change',
    [
        ('different', 'none'),
        ('repriced', 'none'),
        ('same', 'none'),
        ('legacy', 'none'),
        ('different', 'cart'),
        ('different', 'quote_fsm'),
        ('different', 'write_fsm'),
    ],
)
async def test_period_selection_preserves_cart_permissions_and_atomic_fsm(carts, monkeypatch, selection, change):
    from aiogram.fsm.storage.redis import RedisStorage

    service, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-selection-identity-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original = FSMContext(RedisStorage(client, key_builder=builder), key)
    data = {
        'gift_checkout_id': 'old',
        'gift_tariff_id': 2 if selection == 'different' else 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
    }
    if selection != 'legacy':
        data['gift_checkout_selection'] = [1, 30]
    await original.set_state(gift.GiftPurchaseStates.selecting_period)
    await original.set_data(data)
    await service.save_user_cart(uid, {'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
    await service.save_user_cart(
        uid,
        {
            'cart_mode': 'gift_purchase',
            'gift_checkout_id': 'old',
            'tariff_id': 1,
            'period_days': 30,
            'total_price': 100,
            'return_to_cart': True,
        },
    )
    old_cart = await service.get_user_cart(uid)
    await client.pexpire(f'user_cart:{uid}', 30000)
    replacement = None

    async def replace_fsm():
        await original.set_state('new_selection')
        await original.set_data({'gift_checkout_id': 'new'})

    probe = MutationProbe(client, replace_fsm)
    state = FSMContext(RedisStorage(probe, key_builder=builder), key)

    async def quote(*args, **kwargs):
        nonlocal replacement
        if change == 'cart':
            await service.save_user_cart(
                uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'new', 'return_to_cart': True}
            )
            replacement = await service.get_user_cart(uid)
        if change == 'quote_fsm':
            await replace_fsm()
        if change == 'write_fsm':
            probe.armed = True
        return gift.GiftQuote(
            2 if selection == 'different' else 1,
            'Gift',
            30,
            100,
            1,
            200 if selection == 'repriced' else 100,
            200 if selection == 'repriced' else 100,
            0,
            0,
            False,
        )

    monkeypatch.setattr(gift, 'quote_gift_purchase', quote)
    monkeypatch.setattr(gift, 'user_cart_service', service)
    callback = SimpleNamespace(
        data=f'gift_period:{2 if selection == "different" else 1}:30',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    try:
        await gift.handle_gift_period_select(
            callback, SimpleNamespace(id=uid, language='ru', balance_kopeks=1000), AsyncMock(), state
        )
        current = await service.get_user_cart(uid)
        assert await service.has_topup_intent(uid, independent)
        if change == 'cart':
            assert current == replacement
            assert await service.has_topup_intent(uid, current)
            assert await original.get_data() == data
            callback.message.edit_text.assert_not_awaited()
        elif change == 'quote_fsm':
            assert await original.get_data() == {'gift_checkout_id': 'new'}
            assert current == old_cart and await service.has_topup_intent(uid, current)
            callback.message.edit_text.assert_not_awaited()
        else:
            if selection == 'different':
                assert current is None
            else:
                revoked = selection == 'repriced'
                assert current['return_to_cart'] is (not revoked)
                assert await service.has_topup_intent(uid, current) is (not revoked)
                assert 0 < await client.pttl(f'user_cart:{uid}') <= 30000
            if change == 'write_fsm':
                assert probe.triggered
                assert await original.get_state() == 'new_selection'
                assert await original.get_data() == {'gift_checkout_id': 'new'}
                callback.message.edit_text.assert_not_awaited()
            else:
                result = await original.get_data()
                assert (result['gift_checkout_id'] == 'old') is (selection != 'different')
                assert result['gift_checkout_selection'] == [2 if selection == 'different' else 1, 30]
                callback.message.edit_text.assert_awaited_once()
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('action', ['catalog', 'back_tariffs', 'tariff', 'back_periods'])
@pytest.mark.parametrize('change', ['state', 'data', 'both', 'none'])
async def test_navigation_rejects_selection_changed_at_redis_write(carts, monkeypatch, action, change):
    from aiogram.fsm.storage.redis import RedisStorage

    service, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-navigation-write-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    original = FSMContext(RedisStorage(client, key_builder=builder), key)
    data = {
        'gift_checkout_id': 'old',
        'gift_checkout_selection': [1, 30],
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
        'gift_origin_callback': 'menu_subscription',
    }
    await original.set_state(gift.GiftPurchaseStates.confirming_purchase)
    await original.set_data(data)
    await service.save_user_cart(uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'old', 'return_to_cart': True})
    cart = await service.get_user_cart(uid)
    expected_state = (
        'new_selection' if change in ('state', 'both') else gift.GiftPurchaseStates.confirming_purchase.state
    )
    expected_data = {'gift_checkout_id': 'new', 'gift_tariff_id': 2} if change in ('data', 'both') else data

    async def replace():
        if change != 'none':
            await original.set_state(expected_state)
            await original.set_data(expected_data)

    probe = MutationProbe(client, replace)
    state = FSMContext(RedisStorage(probe, key_builder=builder), key)
    quote = gift.GiftQuote(1, 'Gift', 30, 100, 1, 100, 100, 0, 0, False)
    offer = gift.GiftTariffOffer(1, 'Gift', None, 100, 1, 1, (quote,))

    async def offers(*args, **kwargs):
        probe.armed = True
        return [offer]

    monkeypatch.setattr(gift, 'is_gift_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(gift, 'has_sender_gifts', AsyncMock(return_value=False))
    monkeypatch.setattr(gift, 'list_gift_offers', offers)
    payload, handler = {
        'catalog': ('subscription_gift', gift.handle_gift_catalog),
        'back_tariffs': ('gift_back_tariffs', gift.handle_gift_back_tariffs),
        'tariff': ('gift_tariff:1', gift.handle_gift_tariff_select),
        'back_periods': ('gift_back_periods', gift.handle_gift_back_periods),
    }[action]
    callback = SimpleNamespace(data=payload, message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    try:
        await handler(callback, SimpleNamespace(id=uid, language='ru', balance_kopeks=1000), AsyncMock(), state)
        assert probe.triggered
        assert await service.get_user_cart(uid) == cart
        assert await service.has_topup_intent(uid, cart)
        if change != 'none':
            assert await original.get_state() == expected_state
            assert await original.get_data() == expected_data
            callback.message.edit_text.assert_not_awaited()
        else:
            expected = (
                gift.GiftPurchaseStates.selecting_tariff
                if action in ('catalog', 'back_tariffs')
                else gift.GiftPurchaseStates.selecting_period
            )
            assert await original.get_state() == expected.state
            assert await original.get_data() == data
            callback.message.edit_text.assert_awaited_once()
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))


@pytest.mark.parametrize('branch', ['disabled', 'empty', 'missing_tariff'])
async def test_readonly_navigation_does_not_extend_fsm_ttl_or_cart_permission(carts, monkeypatch, branch):
    from aiogram.fsm.storage.redis import RedisStorage

    service, uid, client = carts
    builder = DefaultKeyBuilder(prefix='gift-navigation-ttl-audit', with_bot_id=True)
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    context = FSMContext(RedisStorage(client, key_builder=builder, state_ttl=90, data_ttl=120), key)
    data = {'gift_checkout_id': 'old', 'gift_tariff_id': 1, 'gift_expected_price_kopeks': 100}
    await context.set_state(gift.GiftPurchaseStates.confirming_purchase)
    await context.set_data(data)
    await client.pexpire(builder.build(key, 'state'), 30000)
    await client.pexpire(builder.build(key, 'data'), 20000)
    await service.save_user_cart(uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'old', 'return_to_cart': True})
    cart = await service.get_user_cart(uid)
    monkeypatch.setattr(gift, 'is_gift_enabled', AsyncMock(return_value=branch != 'disabled'))
    monkeypatch.setattr(gift, 'has_sender_gifts', AsyncMock(return_value=False))
    monkeypatch.setattr(gift, 'list_gift_offers', AsyncMock(return_value=[]))
    callback = SimpleNamespace(
        data='gift_tariff:1' if branch == 'missing_tariff' else 'subscription_gift',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    handler = gift.handle_gift_tariff_select if branch == 'missing_tariff' else gift.handle_gift_catalog
    try:
        await handler(callback, SimpleNamespace(id=uid, language='ru', balance_kopeks=1000), AsyncMock(), context)
        assert await context.get_data() == data
        assert await context.get_state() == gift.GiftPurchaseStates.confirming_purchase.state
        assert 0 < await client.pttl(builder.build(key, 'state')) <= 30000
        assert 0 < await client.pttl(builder.build(key, 'data')) <= 20000
        assert await service.get_user_cart(uid) == cart
        assert await service.has_topup_intent(uid, cart)
        callback.message.edit_text.assert_awaited_once()
    finally:
        await client.delete(builder.build(key, 'state'), builder.build(key, 'data'))
