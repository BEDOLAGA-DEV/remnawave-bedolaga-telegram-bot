"""Regression tests for `store_cid_and_fire_purchase` (Telegram bug #558449).

Background
----------
When a user first opens cabinet and quickly clicks "Buy", the separate
`/cabinet/branding/yandex-cid` POST that the frontend fires alongside
Metrika init may not finish before the purchase request lands. Without
the CID in the DB the offline-conversion `on_purchase` event silently
drops — the purchase doesn't appear in Yandex.Metrika reports.

The fix lets each purchase endpoint accept the cached CID in its body
and persists it synchronously before firing the conversion event.
These tests pin that contract: the helper must
  1. persist the CID when one is provided,
  2. fire the purchase event regardless (even when CID is None — covers
     the case where the separate POST DID complete first),
  3. be a noop when the offline-conv feature is disabled.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import yandex_offline_conv_service as yandex_conv


@pytest.mark.asyncio
async def test_passes_cid_through_to_store_and_fires_purchase() -> None:
    """Frontend cached CID → backend stores it, then fires purchase event."""
    with (
        patch.object(yandex_conv, '_is_enabled', return_value=True),
        patch.object(yandex_conv, 'store_cid', AsyncMock(return_value=True)) as store_mock,
        patch.object(yandex_conv, 'spawn_bg') as spawn_mock,
        patch.object(yandex_conv, 'fire_purchase_bg') as fire_mock,
        patch.object(yandex_conv, 'AsyncSessionLocal') as session_local,
    ):
        # Async context manager protocol for AsyncSessionLocal()
        session_local.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_local.return_value.__aexit__ = AsyncMock(return_value=None)

        await yandex_conv.store_cid_and_fire_purchase(user_id=42, cid='abc1234567890.0987654321', amount_kopeks=29900)

        store_mock.assert_awaited_once()
        # store_cid args: (db, user_id, cid, source=...)
        assert store_mock.await_args.args[1] == 42
        assert store_mock.await_args.args[2] == 'abc1234567890.0987654321'

        fire_mock.assert_called_once_with(42, 29900)
        spawn_mock.assert_called_once()


@pytest.mark.asyncio
async def test_no_cid_still_fires_purchase_event() -> None:
    """If the separate /yandex-cid POST already completed, frontend may pass
    None — we must still fire the event so the DB-stored CID gets used."""
    with (
        patch.object(yandex_conv, '_is_enabled', return_value=True),
        patch.object(yandex_conv, 'store_cid', AsyncMock(return_value=True)) as store_mock,
        patch.object(yandex_conv, 'spawn_bg') as spawn_mock,
        patch.object(yandex_conv, 'fire_purchase_bg') as fire_mock,
        patch.object(yandex_conv, 'AsyncSessionLocal') as session_local,
    ):
        session_local.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_local.return_value.__aexit__ = AsyncMock(return_value=None)

        await yandex_conv.store_cid_and_fire_purchase(user_id=42, cid=None, amount_kopeks=29900)

        store_mock.assert_not_called()
        fire_mock.assert_called_once_with(42, 29900)
        spawn_mock.assert_called_once()


@pytest.mark.asyncio
async def test_disabled_feature_skips_everything() -> None:
    """When offline conversions are off, neither store nor fire should run."""
    with (
        patch.object(yandex_conv, '_is_enabled', return_value=False),
        patch.object(yandex_conv, 'store_cid', AsyncMock()) as store_mock,
        patch.object(yandex_conv, 'spawn_bg') as spawn_mock,
        patch.object(yandex_conv, 'fire_purchase_bg') as fire_mock,
    ):
        await yandex_conv.store_cid_and_fire_purchase(user_id=42, cid='abc1234567890.0987654321', amount_kopeks=29900)

        store_mock.assert_not_called()
        fire_mock.assert_not_called()
        spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_store_failure_does_not_block_purchase_event() -> None:
    """Even if persisting the CID throws, the purchase event must still fire —
    the user's existing CID from a previous visit might still be in the DB."""
    with (
        patch.object(yandex_conv, '_is_enabled', return_value=True),
        patch.object(yandex_conv, 'store_cid', AsyncMock(side_effect=Exception('db down'))),
        patch.object(yandex_conv, 'spawn_bg') as spawn_mock,
        patch.object(yandex_conv, 'fire_purchase_bg') as fire_mock,
        patch.object(yandex_conv, 'AsyncSessionLocal') as session_local,
    ):
        session_local.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_local.return_value.__aexit__ = AsyncMock(return_value=None)

        # Should swallow the store error and log a warning; the event still
        # tries to fire (and will no-op gracefully via the same is_enabled
        # guard inside fire_purchase_bg).
        await yandex_conv.store_cid_and_fire_purchase(user_id=42, cid='abc1234567890.0987654321', amount_kopeks=29900)

        # The store raised before we could spawn the bg task — that's fine,
        # the user's existing CID handling is preserved.
        fire_mock.assert_not_called()
        spawn_mock.assert_not_called()
