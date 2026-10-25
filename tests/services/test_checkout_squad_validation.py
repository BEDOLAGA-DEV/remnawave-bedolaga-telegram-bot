"""A failed catalog is not evidence that a paid tariff lost all its squads."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    ('requested', 'catalog', 'blocked'),
    [
        (['masked-1'], [], True),
        (['masked-1'], [SimpleNamespace(uuid='masked-2')], True),
        (['masked-1', 'masked-2'], [SimpleNamespace(uuid='masked-2')], False),
        (['masked-1'], [SimpleNamespace(uuid='masked-1')], False),
        (['masked-1'], None, False),
        (['masked-1'], [{}], False),
        (['masked-1'], [SimpleNamespace(uuid='')], False),
        ([], [], False),
    ],
)
async def test_only_fresh_valid_catalog_can_block_checkout(monkeypatch, requested, catalog, blocked):
    from app.services import checkout_squad_validation as module

    original = list(requested)

    async def get_catalog():
        return catalog

    @asynccontextmanager
    async def api():
        yield SimpleNamespace(get_internal_squads=get_catalog)

    monkeypatch.setattr(module, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=api))
    if blocked:
        with pytest.raises(module.CheckoutSquadsUnavailableError):
            await module.validate_checkout_squads(requested)
    else:
        await module.validate_checkout_squads(requested)
    assert requested == original


@pytest.mark.parametrize('failure', [RuntimeError('panel unavailable'), TimeoutError('panel timeout')])
async def test_catalog_failure_preserves_existing_outage_checkout_semantics(monkeypatch, failure):
    from app.services import checkout_squad_validation as module

    @asynccontextmanager
    async def api():
        raise failure
        yield

    monkeypatch.setattr(module, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=api))
    await module.validate_checkout_squads(['masked-1'])


async def test_preflight_is_bounded(monkeypatch):
    from app.services import checkout_squad_validation as module

    cancelled = False

    async def get_catalog():
        nonlocal cancelled
        try:
            await asyncio.Future()
        finally:
            cancelled = True

    @asynccontextmanager
    async def api():
        yield SimpleNamespace(get_internal_squads=get_catalog)

    monkeypatch.setattr(module, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=api))
    monkeypatch.setattr(module, 'PREFLIGHT_TIMEOUT_SECONDS', 0.001)
    await asyncio.wait_for(module.validate_checkout_squads(['masked-1']), timeout=1)
    assert cancelled


async def test_cancellation_propagates(monkeypatch):
    from app.services import checkout_squad_validation as module

    @asynccontextmanager
    async def api():
        raise asyncio.CancelledError
        yield

    monkeypatch.setattr(module, 'RemnaWaveService', lambda: SimpleNamespace(get_api_client=api))
    with pytest.raises(asyncio.CancelledError):
        await module.validate_checkout_squads(['masked-1'])
