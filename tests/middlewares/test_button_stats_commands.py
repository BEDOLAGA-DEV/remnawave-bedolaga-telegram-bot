"""Тесты логирования команд бота в ButtonStatsMiddleware (/start и т.п.)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import settings
from app.middlewares.button_stats import ButtonStatsMiddleware


def _capture_log_calls(middleware, monkeypatch):
    """Подменяет фоновую запись: возвращает список kwargs вызовов."""
    calls = []

    def fake_log(**kwargs):
        calls.append(kwargs)

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(middleware, '_log_button_click_async', fake_log)
    return calls


def _message(text):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=922920255))


def test_start_command_logged(monkeypatch):
    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)
    middleware = ButtonStatsMiddleware()
    calls = _capture_log_calls(middleware, monkeypatch)

    with patch('app.middlewares.button_stats.asyncio.create_task', MagicMock(side_effect=lambda coro: coro.close())):
        middleware._log_command(_message('/start'))

    assert len(calls) == 1
    assert calls[0]['button_id'] == '/start'
    assert calls[0]['button_type'] == 'command'
    assert calls[0]['button_text'] == '/start'
    assert calls[0]['callback_data'] is None
    assert calls[0]['user_id'] == 922920255


def test_command_payload_not_stored(monkeypatch):
    """Payload диплинков (webauth_/GIFT_/coupon_ токены) не должен попадать в лог."""
    middleware = ButtonStatsMiddleware()
    calls = _capture_log_calls(middleware, monkeypatch)

    with patch('app.middlewares.button_stats.asyncio.create_task', MagicMock(side_effect=lambda coro: coro.close())):
        middleware._log_command(_message('/start webauth_SECRET_TOKEN_123'))

    assert len(calls) == 1
    serialized = str(calls[0])
    assert 'SECRET_TOKEN' not in serialized
    assert calls[0]['button_id'] == '/start'
    assert calls[0]['button_text'] == '/start …'


def test_command_with_bot_mention_normalized(monkeypatch):
    middleware = ButtonStatsMiddleware()
    calls = _capture_log_calls(middleware, monkeypatch)

    with patch('app.middlewares.button_stats.asyncio.create_task', MagicMock(side_effect=lambda coro: coro.close())):
        middleware._log_command(_message('/menu@my_vpn_bot'))

    assert calls[0]['button_id'] == '/menu'


def test_plain_text_not_logged(monkeypatch):
    """Обычные сообщения (промокоды, переписка с поддержкой) не логируются."""
    middleware = ButtonStatsMiddleware()
    calls = _capture_log_calls(middleware, monkeypatch)

    with patch('app.middlewares.button_stats.asyncio.create_task', MagicMock(side_effect=lambda coro: coro.close())):
        middleware._log_command(_message('привет, мой промокод SUMMER25'))
        middleware._log_command(_message(None))
        middleware._log_command(_message('/'))

    assert calls == []


def test_middleware_registered_for_messages():
    """Пин: middleware подключён и к message-апдейтам (иначе команды не видны)."""
    source = (Path(__file__).resolve().parents[2] / 'app' / 'bot.py').read_text(encoding='utf-8')
    assert 'dp.callback_query.middleware(button_stats_middleware)' in source
    assert 'dp.message.middleware(button_stats_middleware)' in source
