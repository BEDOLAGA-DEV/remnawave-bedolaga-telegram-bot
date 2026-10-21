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
    uid = int(uuid4().hex[:12], 16)
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
