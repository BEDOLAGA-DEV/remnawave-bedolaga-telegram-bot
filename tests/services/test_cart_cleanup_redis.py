"""Opt-in cleanup races against an isolated real Redis-compatible server."""

import json
import os
from copy import deepcopy
from types import SimpleNamespace
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
            f'user_cart:{uid}', f'user_cart:{uid}:sub:10', f'user_cart:{uid}:sub:20', f'cart_topup_intent:{uid}'
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
