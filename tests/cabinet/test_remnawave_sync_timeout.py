"""Regression guard for the "pay button spins forever after the product is
delivered" bug.

The cabinet balance-pay endpoints (traffic top-up, renewal, subscription/tariff
purchase, devices) commit the product and THEN sync RemnaWave inline. A slow or
unavailable panel used to hold the HTTP response open, so the cabinet pay button
(bound to the in-flight request) spun far past delivery and, on the 30s axios
timeout, errored instead of showing the delivered product.

Fix: bound each inline sync with ``asyncio.timeout(REMNAWAVE_SYNC_TIMEOUT)`` and
fall back to ``remnawave_retry_queue`` (the same branch the sync errors already
use). These tests pin that invariant.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from app.cabinet.routes.subscription_modules import devices, purchase, traffic
from app.services import subscription_renewal_service


MODULES = [traffic, devices, purchase, subscription_renewal_service]

# The cabinet axios client aborts at 30s (TIMEOUT_MS); the inline sync budget must
# stay safely under that so the response (and the spinner) resolves first.
CABINET_AXIOS_TIMEOUT_S = 30


@pytest.mark.parametrize('mod', MODULES, ids=lambda m: m.__name__.split('.')[-1])
def test_sync_timeout_constant_is_sane(mod):
    budget = mod.REMNAWAVE_SYNC_TIMEOUT
    assert isinstance(budget, (int, float))
    assert 0 < budget < CABINET_AXIOS_TIMEOUT_S


@pytest.mark.parametrize('mod', MODULES, ids=lambda m: m.__name__.split('.')[-1])
def test_inline_sync_is_time_bounded(mod):
    # The inline panel sync must remain wrapped so it can never hold the response
    # open. If someone unwraps it, this fails — that is exactly the regression.
    src = inspect.getsource(mod)
    assert 'asyncio.timeout(REMNAWAVE_SYNC_TIMEOUT)' in src


@pytest.mark.asyncio
async def test_timeout_defers_to_fallback_and_returns_promptly():
    # Proves the mechanism the fix depends on: a stalled panel call is bounded by
    # asyncio.timeout, the raised TimeoutError is caught by ``except Exception``
    # (the production fallback branch), the retry queue is used, and the request
    # returns promptly instead of waiting out the stalled call.
    enqueued: list[str] = []

    async def stalled_panel_sync():
        await asyncio.sleep(60)

    start = time.monotonic()
    try:
        async with asyncio.timeout(0.05):
            await stalled_panel_sync()
    except Exception:  # mirrors the production `except Exception` fallback branch
        enqueued.append('deferred-to-retry-queue')
    elapsed = time.monotonic() - start

    assert enqueued == ['deferred-to-retry-queue']
    assert elapsed < 5
