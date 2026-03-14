"""Тесты для функций сохранения/получения pending_start_payload в channel_checker."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from aiogram.types import Message


class TestRedisPayloadFunctions:
    """Тесты для Redis-функций сохранения payload."""

    async def test_save_pending_payload_to_redis_success(self, monkeypatch):
        """Тест успешного сохранения payload в Redis."""
        from app.middlewares import channel_checker

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis.aclose = AsyncMock()

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(return_value=mock_redis)

            result = await channel_checker.save_pending_payload_to_redis(123456, 'ref_test123')

            assert result is True
            mock_redis.set.assert_awaited_once()
            call_args = mock_redis.set.await_args
            assert 'pending_start_payload:123456' in call_args.args[0]
            assert call_args.args[1] == 'ref_test123'
            assert call_args.kwargs.get('ex') == 3600
            mock_redis.aclose.assert_awaited_once()

    async def test_save_pending_payload_to_redis_failure(self, monkeypatch):
        """Тест обработки ошибки при сохранении в Redis."""
        from app.middlewares import channel_checker

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(side_effect=Exception('Redis connection failed'))

            result = await channel_checker.save_pending_payload_to_redis(123456, 'ref_test123')

            assert result is False

    async def test_get_pending_payload_from_redis_success(self, monkeypatch):
        """Тест успешного получения payload из Redis."""
        from app.middlewares import channel_checker

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b'ref_test123')
        mock_redis.aclose = AsyncMock()

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(return_value=mock_redis)

            result = await channel_checker.get_pending_payload_from_redis(123456)

            assert result == 'ref_test123'
            mock_redis.get.assert_awaited_once()
            mock_redis.aclose.assert_awaited_once()

    async def test_get_pending_payload_from_redis_not_found(self, monkeypatch):
        """Тест когда payload не найден в Redis."""
        from app.middlewares import channel_checker

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.aclose = AsyncMock()

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(return_value=mock_redis)

            result = await channel_checker.get_pending_payload_from_redis(123456)

            assert result is None

    async def test_get_pending_payload_from_redis_failure(self, monkeypatch):
        """Тест обработки ошибки при получении из Redis."""
        from app.middlewares import channel_checker

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(side_effect=Exception('Redis connection failed'))

            result = await channel_checker.get_pending_payload_from_redis(123456)

            assert result is None

    async def test_delete_pending_payload_from_redis(self, monkeypatch):
        """Тест удаления payload из Redis."""
        from app.middlewares import channel_checker

        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(return_value=1)
        mock_redis.aclose = AsyncMock()

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(return_value=mock_redis)

            # Не должно бросать исключение
            await channel_checker.delete_pending_payload_from_redis(123456)

            mock_redis.delete.assert_awaited_once()

    async def test_delete_pending_payload_from_redis_handles_error(self, monkeypatch):
        """Тест что удаление не бросает исключение при ошибке."""
        from app.middlewares import channel_checker

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(side_effect=Exception('Redis error'))

            # Не должно бросать исключение
            await channel_checker.delete_pending_payload_from_redis(123456)


def _create_mock_message(text: str, user_id: int):
    """Создаёт мок Message с нужными атрибутами."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.text = text
    mock_msg.from_user = SimpleNamespace(id=user_id)
    return mock_msg


class TestCaptureStartPayload:
    """Тесты для метода _capture_start_payload."""

    async def test_capture_saves_to_fsm_state(self, monkeypatch):
        """Тест сохранения payload в FSM state."""
        from app.middlewares.channel_checker import ChannelCheckerMiddleware

        middleware = ChannelCheckerMiddleware()

        mock_state = AsyncMock()
        mock_state.get_data = AsyncMock(return_value={})
        mock_state.set_data = AsyncMock()

        mock_message = _create_mock_message('/start ref_abc123', 123456)

        with patch(
            'app.middlewares.channel_checker.save_pending_payload_to_redis', new_callable=AsyncMock
        ) as mock_save_redis:
            await middleware._capture_start_payload(mock_state, mock_message, None)

            mock_state.set_data.assert_awaited_once()
            saved_data = mock_state.set_data.await_args.args[0]
            assert saved_data['pending_start_payload'] == 'ref_abc123'

            # Также должен сохраняться в Redis
            mock_save_redis.assert_awaited_once_with(123456, 'ref_abc123')

    async def test_capture_saves_to_redis_when_state_none(self, monkeypatch):
        """Тест сохранения payload в Redis когда FSM state недоступен."""
        from app.middlewares.channel_checker import ChannelCheckerMiddleware

        middleware = ChannelCheckerMiddleware()

        mock_message = _create_mock_message('/start ref_xyz789', 999888)

        with patch(
            'app.middlewares.channel_checker.save_pending_payload_to_redis', new_callable=AsyncMock
        ) as mock_save_redis:
            await middleware._capture_start_payload(None, mock_message, None)

            # Должен сохраняться в Redis даже если state=None
            mock_save_redis.assert_awaited_once_with(999888, 'ref_xyz789')

    async def test_capture_ignores_message_without_payload(self, monkeypatch):
        """Тест что сообщение без payload игнорируется."""
        from app.middlewares.channel_checker import ChannelCheckerMiddleware

        middleware = ChannelCheckerMiddleware()

        mock_state = AsyncMock()
        mock_state.get_data = AsyncMock(return_value={})
        mock_state.set_data = AsyncMock()

        mock_message = _create_mock_message('/start', 123456)  # Без payload

        with patch(
            'app.middlewares.channel_checker.save_pending_payload_to_redis', new_callable=AsyncMock
        ) as mock_save_redis:
            await middleware._capture_start_payload(mock_state, mock_message, None)

            mock_state.set_data.assert_not_awaited()
            mock_save_redis.assert_not_awaited()

    async def test_capture_ignores_non_start_message(self, monkeypatch):
        """Тест что не-start сообщения игнорируются."""
        from app.middlewares.channel_checker import ChannelCheckerMiddleware

        middleware = ChannelCheckerMiddleware()

        mock_state = AsyncMock()
        mock_state.get_data = AsyncMock(return_value={})
        mock_state.set_data = AsyncMock()

        mock_message = _create_mock_message('/help something', 123456)  # Не /start

        with patch(
            'app.middlewares.channel_checker.save_pending_payload_to_redis', new_callable=AsyncMock
        ) as mock_save_redis:
            await middleware._capture_start_payload(mock_state, mock_message, None)

            mock_state.set_data.assert_not_awaited()
            mock_save_redis.assert_not_awaited()

    async def test_capture_does_not_overwrite_same_payload(self, monkeypatch):
        """Тест что одинаковый payload не перезаписывается в FSM state."""
        from app.middlewares.channel_checker import ChannelCheckerMiddleware

        middleware = ChannelCheckerMiddleware()

        mock_state = AsyncMock()
        mock_state.get_data = AsyncMock(return_value={'pending_start_payload': 'ref_same'})
        mock_state.set_data = AsyncMock()

        mock_message = _create_mock_message('/start ref_same', 123456)  # Тот же payload

        with patch(
            'app.middlewares.channel_checker.save_pending_payload_to_redis', new_callable=AsyncMock
        ) as mock_save_redis:
            await middleware._capture_start_payload(mock_state, mock_message, None)

            # FSM state не должен перезаписываться
            mock_state.set_data.assert_not_awaited()
            # Но в Redis всё равно сохраняем (для надёжности)
            mock_save_redis.assert_awaited_once()


class TestPayloadIntegration:
    """Интеграционные тесты для потока сохранения/восстановления payload."""

    async def test_full_flow_fsm_state_works(self, monkeypatch):
        """Тест полного потока когда FSM state работает корректно."""
        from app.middlewares.channel_checker import ChannelCheckerMiddleware

        middleware = ChannelCheckerMiddleware()

        # Сохраняем payload
        state_storage = {}

        mock_state = AsyncMock()
        mock_state.get_data = AsyncMock(return_value=state_storage)
        mock_state.set_data = AsyncMock(side_effect=state_storage.update)

        mock_message = _create_mock_message('/start ref_flow_test', 111222)

        with patch('app.middlewares.channel_checker.save_pending_payload_to_redis', new_callable=AsyncMock):
            await middleware._capture_start_payload(mock_state, mock_message, None)

        # Проверяем что payload сохранён
        assert state_storage.get('pending_start_payload') == 'ref_flow_test'

    async def test_payload_retrieved_from_redis_fallback(self, monkeypatch):
        """Тест что payload восстанавливается из Redis если в FSM state его нет."""
        from app.middlewares.channel_checker import get_pending_payload_from_redis

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b'ref_from_redis')
        mock_redis.aclose = AsyncMock()

        with patch('app.middlewares.channel_checker.aioredis') as mock_aioredis:
            mock_aioredis.from_url = MagicMock(return_value=mock_redis)

            result = await get_pending_payload_from_redis(333444)

            assert result == 'ref_from_redis'
