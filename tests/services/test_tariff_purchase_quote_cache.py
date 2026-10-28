from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import tariff_purchase_quote as quotes


@pytest.fixture
def quote_cache(monkeypatch):
    stored = {}

    async def save(key, value, **kwargs):
        stored[key] = value
        return True

    cache = AsyncMock()
    cache.set.side_effect = save
    cache.get.side_effect = lambda key: stored.get(key)
    monkeypatch.setattr(quotes, 'cache', cache)
    return cache, stored


@pytest.mark.asyncio
async def test_quote_bound_to_owner_and_channel(quote_cache):
    quote_id, expires = await quotes.save_purchase_quote(1, 'cabinet', {'devices': 3})
    assert await quotes.load_purchase_quote(1, 'cabinet', quote_id) == {'devices': 3}
    for owner, channel in [(2, 'cabinet'), (1, 'telegram')]:
        with pytest.raises(quotes.PurchaseQuoteError, match='quote_expired'):
            await quotes.load_purchase_quote(owner, channel, quote_id)
    assert expires > datetime.now(UTC)
    assert quote_cache[0].set.call_args.kwargs['expire'] == 300


@pytest.mark.asyncio
async def test_cache_failure_does_not_issue_unverifiable_quote(quote_cache):
    quote_cache[0].set.side_effect = None
    quote_cache[0].set.return_value = False
    with pytest.raises(quotes.PurchaseQuoteError, match='quote_unavailable'):
        await quotes.save_purchase_quote(1, 'cabinet', {})


@pytest.mark.asyncio
async def test_expiry_checked_even_if_cache_retains_old_value(quote_cache):
    quote_id, _ = await quotes.save_purchase_quote(1, 'cabinet', {}, now=datetime.now(UTC) - timedelta(minutes=6))
    with pytest.raises(quotes.PurchaseQuoteError, match='quote_expired'):
        await quotes.load_purchase_quote(1, 'cabinet', quote_id)


@pytest.mark.asyncio
async def test_unknown_quote_is_not_a_purchase_authorization(quote_cache):
    with pytest.raises(quotes.PurchaseQuoteError, match='quote_expired'):
        await quotes.load_purchase_quote(1, 'cabinet', uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, [], {}, {'version': 999}, {'version': 1, 'expires_at': 'bad'}])
async def test_malformed_cache_fails_closed(quote_cache, value):
    cache, _ = quote_cache
    cache.get.side_effect = None
    cache.get.return_value = value
    with pytest.raises(quotes.PurchaseQuoteError, match='quote_expired'):
        await quotes.load_purchase_quote(1, 'cabinet', uuid4())
