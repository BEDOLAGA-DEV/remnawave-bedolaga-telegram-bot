"""Tests for pending_start_payload save/get in channel_checker.

After a refactor, payload storage moved from a direct `aioredis.from_url(...)`
client to the shared `app.utils.cache.cache` singleton. Tests now patch the
cache singleton instead of the deleted `aioredis` symbol.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class TestPayloadFunctions:
    """The save/get/delete trio talks to `cache` — patch that, not aioredis."""

    @pytest.mark.asyncio
    async def test_save_pending_payload_to_cache_success(self) -> None:
        from app.middlewares import channel_checker

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.set = AsyncMock(return_value=True)
            result = await channel_checker.save_pending_payload_to_redis(123456, 'ref_test123')
            assert result is True
            mock_cache.set.assert_awaited_once()
            call_args = mock_cache.set.await_args
            assert call_args.args[0] == 'pending_start_payload:123456'
            assert call_args.args[1] == 'ref_test123'
            assert call_args.kwargs.get('expire') == 3600

    @pytest.mark.asyncio
    async def test_save_pending_payload_returns_false_on_cache_error(self) -> None:
        from app.middlewares import channel_checker

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.set = AsyncMock(side_effect=Exception('cache down'))
            result = await channel_checker.save_pending_payload_to_redis(123456, 'ref_test123')
            assert result is False

    @pytest.mark.asyncio
    async def test_get_pending_payload_returns_value(self) -> None:
        from app.middlewares import channel_checker

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.get = AsyncMock(return_value='ref_test123')
            result = await channel_checker.get_pending_payload_from_redis(123456)
            assert result == 'ref_test123'
            mock_cache.get.assert_awaited_once_with('pending_start_payload:123456')

    @pytest.mark.asyncio
    async def test_get_pending_payload_returns_none_when_missing(self) -> None:
        from app.middlewares import channel_checker

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.get = AsyncMock(return_value=None)
            result = await channel_checker.get_pending_payload_from_redis(123456)
            assert result is None

    @pytest.mark.asyncio
    async def test_get_pending_payload_swallows_cache_errors(self) -> None:
        from app.middlewares import channel_checker

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.get = AsyncMock(side_effect=Exception('cache down'))
            result = await channel_checker.get_pending_payload_from_redis(123456)
            assert result is None

    @pytest.mark.asyncio
    async def test_delete_pending_payload_calls_cache_delete(self) -> None:
        from app.middlewares import channel_checker

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.delete = AsyncMock()
            await channel_checker.delete_pending_payload_from_redis(123456)
            mock_cache.delete.assert_awaited_once_with('pending_start_payload:123456')

    @pytest.mark.asyncio
    async def test_delete_pending_payload_swallows_errors(self) -> None:
        from app.middlewares import channel_checker

        with patch.object(channel_checker, 'cache') as mock_cache:
            mock_cache.delete = AsyncMock(side_effect=Exception('cache down'))
            # Must not raise
            await channel_checker.delete_pending_payload_from_redis(123456)
