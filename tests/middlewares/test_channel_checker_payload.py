"""Tests for pending_start_payload save/get in channel_checker.

After a refactor, payload storage moved from a direct `aioredis.from_url(...)`
client to the shared `app.utils.cache.cache` singleton. Tests now patch the
cache singleton instead of the deleted `aioredis` symbol.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class TestPayloadFunctions:
    """Тесты для функций сохранения/получения/удаления payload.

    Union of fork ("ours") and upstream ("theirs") coverage. Both sides patch
    the shared ``cache`` singleton; fork uses ``MagicMock`` + the dotted patch
    target, upstream uses ``patch.object`` + ``@pytest.mark.asyncio``. Both
    variants are kept so neither side loses coverage.
    """

    # -- ours: fork test cases ------------------------------------------------

    async def test_save_pending_payload_to_redis_success(self, monkeypatch):
        from app.middlewares import channel_checker

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=True)

        with patch('app.middlewares.channel_checker.cache', mock_cache):
            result = await channel_checker.save_pending_payload_to_redis(123456, 'ref_test123')
            assert result is True
            mock_cache.set.assert_awaited_once()
            call_args = mock_cache.set.await_args
            assert 'pending_start_payload:123456' in call_args.args[0]
            assert call_args.args[1] == 'ref_test123'
            assert call_args.kwargs.get('expire') == 3600

    async def test_save_pending_payload_to_redis_failure(self, monkeypatch):
        from app.middlewares import channel_checker

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(side_effect=Exception('Redis connection failed'))

        with patch('app.middlewares.channel_checker.cache', mock_cache):
            result = await channel_checker.save_pending_payload_to_redis(123456, 'ref_test123')
            assert result is False

    # -- theirs: upstream test cases ------------------------------------------

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

    # -- ours: fork test cases ------------------------------------------------

    async def test_get_pending_payload_from_redis_success(self, monkeypatch):
        from app.middlewares import channel_checker

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value='ref_test123')

        with patch('app.middlewares.channel_checker.cache', mock_cache):
            result = await channel_checker.get_pending_payload_from_redis(123456)
            assert result == 'ref_test123'
            mock_cache.get.assert_awaited_once()

    async def test_get_pending_payload_from_redis_not_found(self, monkeypatch):
        from app.middlewares import channel_checker

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)

        with patch('app.middlewares.channel_checker.cache', mock_cache):
            result = await channel_checker.get_pending_payload_from_redis(123456)
            assert result is None

    async def test_get_pending_payload_from_redis_failure(self, monkeypatch):
        from app.middlewares import channel_checker

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(side_effect=Exception('Redis connection failed'))

        with patch('app.middlewares.channel_checker.cache', mock_cache):
            result = await channel_checker.get_pending_payload_from_redis(123456)
            assert result is None

    # -- theirs: upstream test cases ------------------------------------------

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

    # -- ours: fork test cases ------------------------------------------------

    async def test_delete_pending_payload_from_redis(self, monkeypatch):
        from app.middlewares import channel_checker

        mock_cache = MagicMock()
        mock_cache.delete = AsyncMock(return_value=1)

        with patch('app.middlewares.channel_checker.cache', mock_cache):
            await channel_checker.delete_pending_payload_from_redis(123456)
            mock_cache.delete.assert_awaited_once()

    async def test_delete_pending_payload_from_redis_handles_error(self, monkeypatch):
        from app.middlewares import channel_checker

        mock_cache = MagicMock()
        mock_cache.delete = AsyncMock(side_effect=Exception('Redis error'))

        with patch('app.middlewares.channel_checker.cache', mock_cache):
            # Не должно бросать исключение
            await channel_checker.delete_pending_payload_from_redis(123456)

    # -- theirs: upstream test cases ------------------------------------------

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
