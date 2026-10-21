"""Opt-in cleanup races against an isolated real Redis-compatible server."""

import json
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from app.config import settings
from app.services import subscription_auto_purchase_service as auto
from app.services.user_cart_service import UserCartService
from tests.fixtures.gift_callbacks import bind_gift_action
from tests.fixtures.real_redis import ensure_real_redis


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def carts(monkeypatch):
    url = os.environ.get('TEST_CART_REDIS_URL')
    if not url:
        if os.environ.get('REQUIRE_REDIS_TESTS') == '1':
            pytest.fail('TEST_CART_REDIS_URL required')
        pytest.skip('isolated Redis not configured')
    client = ensure_real_redis(monkeypatch).create_redis(url)
    await client.ping()
    service = UserCartService()
    service._initialized = True
    service._redis_client = client
    monkeypatch.setattr(auto, 'user_cart_service', service)
    uid = int(uuid4().hex[:7], 16)
    try:
        yield service, uid, client
    finally:
        await client.delete(
            f'user_cart:{uid}',
            f'user_cart:{uid}:sub:10',
            f'user_cart:{uid}:sub:20',
            f'cart_topup_intent:{uid}',
            f'cart_topup_intent:{uid}:sub:10',
            f'cart_topup_intent:{uid}:sub:20',
        )
        await client.aclose()


@pytest.mark.parametrize('source,target', [(10, 10), (10, 20), (None, 10), (10, None), (None, None)])
@pytest.mark.parametrize('mode', ['tariff_purchase', 'daily_tariff_purchase', 'extend'])
async def test_finished_cart_does_not_delete_replacement(carts, source, target, mode):
    service, uid, _ = carts
    old = {'cart_mode': mode, 'subscription_id': source, 'tariff_id': 1, 'period_days': 30, 'return_to_cart': True}
    assert await service.save_user_cart(uid, old)
    processed = deepcopy(await service.get_user_cart(uid))
    new = {'cart_mode': mode, 'subscription_id': target, 'tariff_id': 2, 'period_days': 60, 'return_to_cart': True}
    assert await service.save_user_cart(uid, new)
    replacement = await service.get_user_cart(uid)
    await auto._delete_cart_for_subscription(uid, processed)
    assert await service.get_user_cart(uid) == replacement
    if target is not None:
        assert await service.get_subscription_cart(uid, target) == replacement
    if source is not None and source != target:
        assert await service.get_subscription_cart(uid, source) is None
    assert await service.has_topup_intent(uid)


async def test_completed_global_and_subscription_aliases_are_both_removed(carts):
    service, uid, _ = carts
    cart = {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'return_to_cart': True}
    assert await service.save_user_cart(uid, cart)
    processed = await service.get_subscription_cart(uid, 10)
    await auto._delete_cart_for_subscription(uid, processed)
    assert await service.get_user_cart(uid) is None
    assert await service.get_subscription_cart(uid, 10) is None
    assert not await service.has_topup_intent(uid)


async def test_new_save_of_same_options_is_preserved(carts):
    service, uid, _ = carts
    cart = {'cart_mode': 'daily_tariff_purchase', 'subscription_id': 10, 'tariff_id': 1, 'return_to_cart': True}
    assert await service.save_user_cart(uid, cart)
    processed = deepcopy(await service.get_user_cart(uid))
    assert await service.save_user_cart(uid, deepcopy(cart))
    replacement = await service.get_user_cart(uid)
    await auto._delete_cart_for_subscription(uid, processed)
    assert await service.get_user_cart(uid) == replacement
    assert await service.get_subscription_cart(uid, 10) == replacement
    assert await service.has_topup_intent(uid)


async def test_dispatch_completion_preserves_new_topup_intent(carts, monkeypatch):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    cart = {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'return_to_cart': True}
    assert await service.save_user_cart(uid, cart)

    async def finish_then_replace(db, user, processed, **kwargs):
        await auto._delete_cart_for_subscription(uid, processed)
        await service.save_user_cart(
            uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 20, 'tariff_id': 2, 'return_to_cart': True}
        )
        return True

    monkeypatch.setattr(auto, '_process_single_cart', finish_then_replace)
    assert await auto.auto_purchase_saved_cart_after_topup(None, SimpleNamespace(id=uid, telegram_id=None))
    assert (await service.get_user_cart(uid))['subscription_id'] == 20
    assert await service.has_topup_intent(uid)


@pytest.mark.parametrize('replacement', [False, True])
async def test_legacy_cart_cleanup_compares_payload(carts, replacement):
    service, uid, client = carts
    old = {'cart_mode': 'extend', 'subscription_id': 10, 'period_days': 30}
    raw = json.dumps(old, ensure_ascii=False)
    await client.set(f'user_cart:{uid}', raw, ex=60)
    await client.set(f'user_cart:{uid}:sub:10', raw, ex=60)
    await client.set(f'cart_topup_intent:{uid}', '1', ex=60)
    if replacement:
        await service.save_user_cart(
            uid, {'cart_mode': 'extend', 'subscription_id': 10, 'period_days': 60, 'return_to_cart': True}
        )
    before = await service.get_user_cart(uid)
    await auto._delete_cart_for_subscription(uid, old)
    if replacement:
        assert await service.get_user_cart(uid) == before
        assert await service.get_subscription_cart(uid, 10) == before
        assert await service.has_topup_intent(uid)
    else:
        assert await service.get_user_cart(uid) is None
        assert await service.get_subscription_cart(uid, 10) is None
        assert not await service.has_topup_intent(uid)


async def test_repriced_revision_still_cleans_its_aliases(carts):
    service, uid, _ = carts
    await service.save_user_cart(
        uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'total_price': 100, 'return_to_cart': True}
    )
    processed = await service.get_user_cart(uid)
    processed['total_price'] = 200
    await auto._delete_cart_for_subscription(uid, processed)
    assert await service.get_user_cart(uid) is None
    assert await service.get_subscription_cart(uid, 10) is None
    assert not await service.has_topup_intent(uid)


async def test_subscription_only_cleanup_consumes_its_own_intent(carts):
    service, uid, _ = carts
    await service.save_user_cart(uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'return_to_cart': True})
    processed = await service.get_subscription_cart(uid, 10)
    await service.delete_global_cart_only(uid)
    await auto._delete_cart_for_subscription(uid, processed)
    assert await service.get_subscription_cart(uid, 10) is None
    assert not await service.has_topup_intent(uid)


class PublicationProbe:
    """Observe server state after a publication, without emulating Redis writes."""

    def __init__(self, client, keys, on_publish=None):
        self.client = client
        self.keys = keys
        self.on_publish = on_publish
        self.snapshots = []

    async def published(self):
        if self.on_publish is not None:
            callback, self.on_publish = self.on_publish, None
            await callback()
        self.snapshots.append(await self.client.mget(self.keys))

    async def setex(self, *args):
        result = await self.client.setex(*args)
        await self.published()
        return result

    def pipeline(self, **kwargs):
        probe = self
        pipeline = self.client.pipeline(**kwargs)

        class ObservedPipeline:
            def setex(self, *args):
                pipeline.setex(*args)

            async def execute(self):
                result = await pipeline.execute()
                await probe.published()
                return result

        return ObservedPipeline()


@pytest.mark.parametrize('subscription_id', [None, 10])
@pytest.mark.parametrize('interleave', [False, True])
async def test_save_publishes_matching_aliases_and_intent(carts, subscription_id, interleave):
    service, uid, client = carts
    old = {'cart_mode': 'tariff_purchase', 'subscription_id': subscription_id, 'tariff_id': 1, 'return_to_cart': True}
    await service.save_user_cart(uid, old)
    keys = [f'user_cart:{uid}', f'cart_topup_intent:{uid}']
    if subscription_id is not None:
        keys.append(f'user_cart:{uid}:sub:{subscription_id}')
    second_service = UserCartService()
    second_service._initialized = True
    second_service._redis_client = client

    async def replace():
        assert await second_service.save_user_cart(uid, dict(old, tariff_id=3))

    probe = PublicationProbe(client, keys, replace if interleave else None)
    service._redis_client = probe
    assert await service.save_user_cart(uid, dict(old, tariff_id=2))
    assert probe.snapshots
    for snapshot in probe.snapshots:
        global_cart = json.loads(snapshot[0])
        assert snapshot[1].decode() == global_cart['_cart_revision']
        if subscription_id is not None:
            assert json.loads(snapshot[2]) == global_cart
    assert (await second_service.get_user_cart(uid))['tariff_id'] == (3 if interleave else 2)


@pytest.mark.parametrize('subscription_id', [None, 10])
async def test_old_intent_does_not_authorize_unconfirmed_replacement(carts, monkeypatch, subscription_id):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    await service.save_user_cart(
        uid,
        {'cart_mode': 'tariff_purchase', 'subscription_id': subscription_id, 'tariff_id': 1, 'return_to_cart': True},
    )
    await service.save_user_cart(
        uid,
        {'cart_mode': 'tariff_purchase', 'subscription_id': subscription_id, 'tariff_id': 2, 'return_to_cart': False},
    )
    process = AsyncMock(return_value=True)
    monkeypatch.setattr(auto, '_process_single_cart', process)
    assert not await auto.auto_purchase_saved_cart_after_topup(None, SimpleNamespace(id=uid, telegram_id=None))
    process.assert_not_awaited()
    assert (await service.get_user_cart(uid))['tariff_id'] == 2


async def test_independently_confirmed_subscription_carts_remain_eligible(carts, monkeypatch):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    for sid in (10, 20):
        await service.save_user_cart(
            uid, {'cart_mode': 'tariff_purchase', 'subscription_id': sid, 'tariff_id': sid, 'return_to_cart': True}
        )
    processed = []

    async def finish(db, user, cart, **kwargs):
        processed.append(cart['subscription_id'])
        await auto._delete_cart_for_subscription(uid, cart)
        return True

    monkeypatch.setattr(auto, '_process_single_cart', finish)
    assert await auto.auto_purchase_saved_cart_after_topup(None, SimpleNamespace(id=uid, telegram_id=None))
    assert sorted(processed) == [10, 20]
    assert not await service.has_topup_intent(uid)


async def test_expired_source_intent_cannot_use_neighbor_intent(carts, monkeypatch):
    service, uid, client = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    for sid in (10, 20):
        await service.save_user_cart(
            uid, {'cart_mode': 'tariff_purchase', 'subscription_id': sid, 'tariff_id': sid, 'return_to_cart': True}
        )
    # Simulate this source's intent TTL ending; neighbor's intent is still fresh.
    await client.delete(f'cart_topup_intent:{uid}:sub:10')
    process = AsyncMock(return_value=False)
    monkeypatch.setattr(auto, '_process_single_cart', process)
    assert not await auto.auto_purchase_saved_cart_after_topup(None, SimpleNamespace(id=uid, telegram_id=None))
    assert [call.args[2]['subscription_id'] for call in process.await_args_list] == [20]


async def test_stale_collected_revision_cannot_use_replacement_intent(carts, monkeypatch):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    await service.save_user_cart(
        uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'tariff_id': 1, 'return_to_cart': True}
    )
    collected = await service.get_subscription_cart(uid, 10)
    await service.save_user_cart(uid, dict(collected, tariff_id=2))
    monkeypatch.setattr(service, 'get_all_subscription_carts', AsyncMock(return_value=[collected]))
    process = AsyncMock(return_value=True)
    monkeypatch.setattr(auto, '_process_single_cart', process)
    assert not await auto.auto_purchase_saved_cart_after_topup(None, SimpleNamespace(id=uid, telegram_id=None))
    process.assert_not_awaited()
    assert (await service.get_user_cart(uid))['tariff_id'] == 2


@pytest.mark.parametrize('confirmed', [False, True])
async def test_legacy_global_intent_requires_explicit_confirmation(carts, monkeypatch, confirmed):
    service, uid, client = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    cart = {'cart_mode': 'tariff_purchase', 'tariff_id': 1, 'return_to_cart': confirmed}
    await client.set(f'user_cart:{uid}', json.dumps(cart, ensure_ascii=False), ex=60)
    await client.set(f'cart_topup_intent:{uid}', '1', ex=60)
    process = AsyncMock(return_value=False)
    monkeypatch.setattr(auto, '_process_single_cart', process)
    assert not await auto.auto_purchase_saved_cart_after_topup(None, SimpleNamespace(id=uid, telegram_id=None))
    assert process.await_count == int(confirmed)


async def test_explicit_intent_clear_revokes_all_sources(carts):
    service, uid, client = carts
    for sid in (10, 20):
        await service.save_user_cart(uid, {'subscription_id': sid, 'return_to_cart': True})
    await service.clear_topup_intent(uid)
    assert not await service.has_topup_intent(uid)
    for sid in (10, 20):
        assert await client.get(f'cart_topup_intent:{uid}:sub:{sid}') is None
        assert not await service.has_topup_intent(uid, await service.get_subscription_cart(uid, sid))


@pytest.mark.parametrize('confirmed_neighbor', [False, True])
async def test_topup_priority_uses_actual_cart_permission(carts, monkeypatch, confirmed_neighbor):
    from app.services.payment import common

    service, uid, _ = carts
    monkeypatch.setattr(common, 'user_cart_service', service)
    if confirmed_neighbor:
        await service.save_user_cart(
            uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'total_price': 100, 'return_to_cart': True}
        )
    await service.save_user_cart(uid, {'cart_mode': 'tariff_purchase', 'total_price': 100, 'return_to_cart': True})
    await service.save_user_cart(uid, {'cart_mode': 'tariff_purchase', 'total_price': 100, 'return_to_cart': False})
    resume = AsyncMock(return_value=True)
    purchase = AsyncMock(return_value=False)
    monkeypatch.setattr(auto, 'try_resume_disabled_daily_after_topup', resume)
    monkeypatch.setattr(auto, 'auto_purchase_saved_cart_after_topup', purchase)
    assert not await common.send_cart_notification_after_topup(
        SimpleNamespace(id=uid, telegram_id=None), 100, None, None, notify_email=False
    )
    assert resume.await_count == int(not confirmed_neighbor)
    assert purchase.await_count == int(confirmed_neighbor)


@pytest.mark.parametrize('replacement_mode', ['gift_purchase', 'tariff_purchase'])
async def test_old_gift_completion_preserves_replacement_and_independent_intent(carts, monkeypatch, replacement_mode):
    service, uid, _ = carts
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    await service.save_user_cart(uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
    await service.save_user_cart(uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'old', 'return_to_cart': True})
    replacement = None

    async def finish(db, user, cart, **kwargs):
        nonlocal replacement
        await service.save_user_cart(
            uid,
            {
                'cart_mode': replacement_mode,
                'gift_checkout_id': 'new',
                'subscription_id': 20 if replacement_mode == 'tariff_purchase' else None,
                'return_to_cart': True,
            },
        )
        replacement = await service.get_user_cart(uid)
        return True

    monkeypatch.setattr(auto, '_auto_purchase_gift', finish)
    assert await auto.auto_purchase_saved_cart_after_topup(None, SimpleNamespace(id=uid, telegram_id=None))
    assert await service.get_user_cart(uid) == replacement
    assert await service.has_topup_intent(uid, replacement)
    assert await service.has_topup_intent(uid, independent)


@pytest.mark.parametrize(
    'failure', ['quote_terminal', 'quote_reprice', 'partial', 'purchase_terminal', 'purchase_reprice']
)
async def test_old_gift_failure_or_update_cannot_overwrite_replacement(carts, monkeypatch, failure):
    from unittest.mock import MagicMock

    from app.services.gift_purchase_service import GiftFeatureDisabledError, GiftPriceChangedError

    service, uid, _ = carts
    await service.save_user_cart(uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
    await service.save_user_cart(
        uid,
        {
            'cart_mode': 'gift_purchase',
            'tariff_id': 1,
            'period_days': 30,
            'total_price': 100,
            'gift_checkout_id': 'old',
            'return_to_cart': True,
        },
    )
    old = await service.get_user_cart(uid)
    replacement = None

    async def replace():
        nonlocal replacement
        await service.save_user_cart(
            uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 20, 'tariff_id': 2, 'return_to_cart': True}
        )
        replacement = await service.get_user_cart(uid)

    async def quote(*args, **kwargs):
        if failure in ('quote_terminal', 'quote_reprice', 'partial'):
            await replace()
        if failure == 'quote_terminal':
            raise GiftFeatureDisabledError
        return SimpleNamespace(final_price_kopeks=200 if failure == 'quote_reprice' else 100)

    async def purchase(**kwargs):
        await replace()
        if failure == 'purchase_terminal':
            raise GiftFeatureDisabledError
        raise GiftPriceChangedError(100, SimpleNamespace(final_price_kopeks=200))

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    monkeypatch.setattr(auto, 'quote_gift_purchase', quote)
    monkeypatch.setattr(auto, 'purchase_gift_from_balance', purchase)
    monkeypatch.setattr(auto, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    assert not await auto._auto_purchase_gift(
        db,
        SimpleNamespace(id=uid, telegram_id=None, language='ru', balance_kopeks=50 if failure == 'partial' else 1000),
        old,
        bot=AsyncMock(),
    )
    assert await service.get_user_cart(uid) == replacement
    assert await service.has_topup_intent(uid, replacement)
    assert await service.has_topup_intent(uid, independent)


@pytest.mark.parametrize('repriced', [False, True])
async def test_gift_metadata_update_preserves_ttl_and_other_source_permission(carts, monkeypatch, repriced):
    from unittest.mock import MagicMock

    service, uid, client = carts
    await service.save_user_cart(uid, {'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
    await service.save_user_cart(
        uid,
        {
            'cart_mode': 'gift_purchase',
            'tariff_id': 1,
            'period_days': 30,
            'total_price': 100,
            'gift_checkout_id': 'old',
            'return_to_cart': True,
        },
    )
    old = await service.get_user_cart(uid)
    await client.pexpire(f'user_cart:{uid}', 30000)
    await client.pexpire(f'cart_topup_intent:{uid}', 20000)
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    monkeypatch.setattr(
        auto,
        'quote_gift_purchase',
        AsyncMock(return_value=SimpleNamespace(final_price_kopeks=200 if repriced else 100)),
    )
    assert not await auto._auto_purchase_gift(
        db, SimpleNamespace(id=uid, telegram_id=None, language='ru', balance_kopeks=50), old, bot=AsyncMock()
    )
    current = await service.get_user_cart(uid)
    assert current['total_price'] == (200 if repriced else 100)
    assert current['missing_amount'] == (150 if repriced else 50)
    assert current['return_to_cart'] is (not repriced)
    assert 0 < await client.pttl(f'user_cart:{uid}') <= 30000
    assert await service.has_topup_intent(uid, independent)
    if repriced:
        assert current['_cart_revision'] != old['_cart_revision']
        assert not await service.has_topup_intent(uid, current)
    else:
        assert current['_cart_revision'] == old['_cart_revision']
        assert 0 < await client.pttl(f'cart_topup_intent:{uid}') <= 20000
        assert await service.has_topup_intent(uid, current)


@pytest.mark.parametrize('action', ['cancel', 'confirm', 'terminal_resume'])
@pytest.mark.parametrize('replacement_mode', ['gift_purchase', 'tariff_purchase'])
async def test_manual_gift_cleanup_preserves_replacement(carts, monkeypatch, action, replacement_mode):
    from unittest.mock import MagicMock

    from app.handlers.subscription import gift
    from app.services.gift_purchase_service import GiftFeatureDisabledError

    service, uid, _ = carts
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, 'show_subscription_info', AsyncMock())
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    monkeypatch.setattr(gift, 'build_gift_result_presentation', lambda **kwargs: ('done', None))
    await service.save_user_cart(uid, {'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
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
    original_get = service.get_user_cart
    replacement = None

    async def replace():
        nonlocal replacement
        await service.save_user_cart(
            uid,
            {
                'cart_mode': replacement_mode,
                'gift_checkout_id': 'new',
                'subscription_id': 20 if replacement_mode == 'tariff_purchase' else None,
                'return_to_cart': True,
            },
        )
        replacement = await original_get(uid)

    async def read_then_replace(user_id):
        result = await original_get(user_id)
        if replacement is None:
            await replace()
        return result

    async def purchase(**kwargs):
        await replace()
        return SimpleNamespace()

    async def quote(*args, **kwargs):
        await replace()
        raise GiftFeatureDisabledError

    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=AsyncMock())
    user = SimpleNamespace(id=uid, language='ru', telegram_id=None, balance_kopeks=1000)
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    await state.set_data(
        {
            'gift_checkout_id': 'old',
            'gift_tariff_id': 1,
            'gift_period_days': 30,
            'gift_expected_price_kopeks': 100,
            'gift_origin_callback': 'menu_subscription',
        }
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    if action == 'cancel':
        monkeypatch.setattr(service, 'get_user_cart', read_then_replace)
        await bind_gift_action(callback, state, 'gift_cancel')
        await gift.handle_gift_cancel(callback, user, db, state)
    elif action == 'confirm':
        monkeypatch.setattr(gift, 'purchase_gift_from_balance', purchase)
        await bind_gift_action(callback, state, 'gift_confirm')
        await gift.handle_gift_confirm(callback, user, db, state)
    else:
        monkeypatch.setattr(gift, 'quote_gift_purchase', quote)
        await gift.handle_return_to_gift_cart(callback, user, db, state)
    assert await original_get(uid) == replacement
    assert await service.has_topup_intent(uid, replacement)
    assert await service.has_topup_intent(uid, independent)


async def test_gift_resume_does_not_delete_subscription_cart(carts, monkeypatch):
    from app.handlers.subscription import gift

    service, uid, _ = carts
    monkeypatch.setattr(gift, 'user_cart_service', service)
    await service.save_user_cart(
        uid, {'cart_mode': 'tariff_purchase', 'subscription_id': 20, 'user_id': uid, 'return_to_cart': True}
    )
    original = await service.get_user_cart(uid)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await gift.handle_return_to_gift_cart(callback, SimpleNamespace(id=uid, language='ru'), AsyncMock(), AsyncMock())
    assert await service.get_user_cart(uid) == original
    assert await service.has_topup_intent(uid, original)


@pytest.mark.parametrize(
    'replacement_kind', ['gift', 'subscription', 'gift_cart_only', 'subscription_cart_only', 'fsm', 'none']
)
async def test_manual_gift_shortfall_resume_preserves_new_selection(carts, monkeypatch, replacement_kind):
    from unittest.mock import MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.handlers.subscription import gift

    service, uid, client = carts
    monkeypatch.setattr(gift, 'user_cart_service', service)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    await state.set_state('old_selection')
    await state.set_data({'gift_checkout_id': 'old', 'other': 'keep'})
    await service.save_user_cart(uid, {'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
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
    original = await service.get_user_cart(uid)
    await client.pexpire(f'user_cart:{uid}', 30000)
    await client.pexpire(f'cart_topup_intent:{uid}', 20000)
    replacement = None

    async def quote(*args, **kwargs):
        nonlocal replacement
        if replacement_kind in ('gift', 'subscription', 'gift_cart_only', 'subscription_cart_only'):
            await service.save_user_cart(
                uid,
                {
                    'cart_mode': 'gift_purchase' if replacement_kind.startswith('gift') else 'tariff_purchase',
                    'gift_checkout_id': 'new',
                    'subscription_id': 20 if replacement_kind.startswith('subscription') else None,
                    'return_to_cart': True,
                },
            )
            replacement = await service.get_user_cart(uid)
        if replacement_kind in ('gift', 'subscription', 'fsm'):
            await state.set_state('new_selection')
            await state.set_data({'gift_checkout_id': 'new', 'other': 'new'})
        return SimpleNamespace(final_price_kopeks=100)

    monkeypatch.setattr(gift, 'quote_gift_purchase', quote)
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db.execute.return_value = result
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await gift.handle_return_to_gift_cart(
        callback,
        SimpleNamespace(id=uid, language='ru', balance_kopeks=50),
        db,
        state,
    )
    current = await service.get_user_cart(uid)
    assert await service.has_topup_intent(uid, independent)
    if replacement_kind != 'none':
        assert current == (replacement or original)
        if replacement_kind.endswith('cart_only'):
            assert await state.get_state() == 'old_selection'
            assert await state.get_data() == {'gift_checkout_id': 'old', 'other': 'keep'}
        else:
            assert await state.get_state() == 'new_selection'
            assert await state.get_data() == {'gift_checkout_id': 'new', 'other': 'new'}
        callback.message.edit_text.assert_not_awaited()
    else:
        assert current['_cart_revision'] == original['_cart_revision']
        assert current['missing_amount'] == 50
        assert 0 < await client.pttl(f'user_cart:{uid}') <= 30000
        assert 0 < await client.pttl(f'cart_topup_intent:{uid}') <= 20000
        assert await service.has_topup_intent(uid, current)
        callback.message.edit_text.assert_awaited_once()


@pytest.mark.parametrize('initial', ['absent', 'gift', 'subscription'])
@pytest.mark.parametrize('change', ['none', 'gift', 'subscription', 'fsm'])
async def test_initial_gift_shortfall_does_not_replace_new_checkout(carts, monkeypatch, initial, change):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.handlers.subscription import gift
    from app.services.gift_purchase_service import GiftInsufficientBalanceError

    service, uid, _ = carts
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, 'resolve_gift_claim_channel', AsyncMock(return_value=('test_bot', None)))
    await service.save_user_cart(uid, {'subscription_id': 10, 'return_to_cart': True})
    independent = await service.get_subscription_cart(uid, 10)
    if initial == 'absent':
        await service.delete_global_cart_only(uid)
    else:
        await service.save_user_cart(
            uid,
            {
                'cart_mode': 'gift_purchase' if initial == 'gift' else 'tariff_purchase',
                'gift_checkout_id': 'old',
                'subscription_id': 20 if initial == 'subscription' else None,
                'return_to_cart': True,
            },
        )
    original = await service.get_user_cart(uid)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    selection = {
        'gift_checkout_id': 'old',
        'gift_tariff_id': 1,
        'gift_period_days': 30,
        'gift_expected_price_kopeks': 100,
    }
    await state.set_data(selection)
    await state.set_state('confirming')
    replacement = None

    async def purchase(**kwargs):
        nonlocal replacement
        if change in ('gift', 'subscription'):
            await service.save_user_cart(
                uid,
                {
                    'cart_mode': 'gift_purchase' if change == 'gift' else 'tariff_purchase',
                    'gift_checkout_id': 'new',
                    'subscription_id': 20 if change == 'subscription' else None,
                    'return_to_cart': True,
                },
            )
            replacement = await service.get_user_cart(uid)
        if change == 'fsm':
            await state.set_data({'gift_checkout_id': 'new'})
        raise GiftInsufficientBalanceError(100, 50)

    monkeypatch.setattr(gift, 'purchase_gift_from_balance', purchase)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=AsyncMock())
    await bind_gift_action(callback, state, 'gift_confirm')
    await gift.handle_gift_confirm(callback, SimpleNamespace(id=uid, language='ru'), AsyncMock(), state)
    current = await service.get_user_cart(uid)
    assert await service.has_topup_intent(uid, independent)
    if change != 'none':
        assert current == (replacement if change != 'fsm' else original)
        callback.message.edit_text.assert_not_awaited()
    else:
        assert current['gift_checkout_id'] == 'old'
        assert current['missing_amount'] == 50
        assert await service.has_topup_intent(uid, current)
        callback.message.edit_text.assert_awaited_once()
    assert await state.get_data() == ({'gift_checkout_id': 'new'} if change == 'fsm' else selection)


@pytest.mark.parametrize('original_kind', ['absent', 'legacy', 'versioned'])
@pytest.mark.parametrize('replaced', [False, True])
async def test_conditional_gift_publication_checks_snapshot_at_redis_boundary(carts, original_kind, replaced):
    service, uid, client = carts
    original = None
    if original_kind == 'legacy':
        original = {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'legacy'}
        await client.set(f'user_cart:{uid}', json.dumps(original, ensure_ascii=False), ex=60)
    elif original_kind == 'versioned':
        await service.save_user_cart(uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'original'})
        original = await service.get_user_cart(uid)
    replacement = None
    if replaced:
        await service.save_user_cart(
            uid, {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'new', 'return_to_cart': True}
        )
        replacement = await service.get_user_cart(uid)
    gift_cart = {'cart_mode': 'gift_purchase', 'gift_checkout_id': 'submitted', 'return_to_cart': True}
    assert await service.save_gift_cart_if_current(uid, gift_cart, original) is (not replaced)
    current = await service.get_user_cart(uid)
    if replaced:
        assert current == replacement
        assert '_cart_revision' not in gift_cart
    else:
        assert current == gift_cart
        assert current['gift_checkout_id'] == 'submitted'
    assert await service.has_topup_intent(uid, current)


async def test_conditional_gift_publication_fails_closed_without_redis(carts):
    service, uid, _ = carts
    service._redis_client = None
    assert not await service.save_gift_cart_if_current(
        uid, {'cart_mode': 'gift_purchase', 'return_to_cart': True}, None
    )


@pytest.mark.parametrize('balance', [50, 200])
@pytest.mark.parametrize('replacement_mode', ['gift_purchase', 'tariff_purchase'])
async def test_gift_resume_does_not_restore_cart_replaced_during_quote(carts, monkeypatch, balance, replacement_mode):
    from unittest.mock import MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.handlers.subscription import gift

    service, uid, _ = carts
    monkeypatch.setattr(gift, 'user_cart_service', service)
    monkeypatch.setattr(gift, '_render_confirmation_summary', lambda *args: ('confirm', None))
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=uid, user_id=uid))
    selection = {'gift_checkout_id': 'old'}
    await state.set_state('old_selection')
    await state.set_data(selection)
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
    replacement = None

    async def quote(*args, **kwargs):
        nonlocal replacement
        await service.save_user_cart(
            uid,
            {
                'cart_mode': replacement_mode,
                'gift_checkout_id': 'new',
                'return_to_cart': True,
                'subscription_id': 20 if replacement_mode == 'tariff_purchase' else None,
            },
        )
        replacement = await service.get_user_cart(uid)
        return SimpleNamespace(final_price_kopeks=100)

    monkeypatch.setattr(gift, 'quote_gift_purchase', quote)
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db.execute.return_value = result
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await gift.handle_return_to_gift_cart(
        callback, SimpleNamespace(id=uid, language='ru', balance_kopeks=balance), db, state
    )
    assert await state.get_state() == 'old_selection'
    assert await state.get_data() == selection
    assert await service.get_user_cart(uid) == replacement
    assert await service.has_topup_intent(uid, replacement)
    callback.message.edit_text.assert_not_awaited()
