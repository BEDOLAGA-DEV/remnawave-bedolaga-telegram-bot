"""Тесты лога действий юзера в кабинете (user_action_log_service)."""

from unittest.mock import AsyncMock, patch

from app.config import Settings, settings
from app.services.user_action_log_service import (
    normalize_cabinet_path,
    schedule_cabinet_action_log,
    should_log_cabinet_action,
)


def test_defaults() -> None:
    assert Settings.model_fields['USER_ACTION_LOG_ENABLED'].default is True
    assert Settings.model_fields['USER_ACTION_LOG_RETENTION_DAYS'].default == 90


def test_normalize_cabinet_path() -> None:
    assert normalize_cabinet_path('/cabinet/subscriptions/5/renew') == '/cabinet/subscriptions/{id}/renew'
    assert normalize_cabinet_path('/cabinet/tickets/123') == '/cabinet/tickets/{id}'
    assert normalize_cabinet_path('/cabinet/balance/topup') == '/cabinet/balance/topup'


def test_should_log_gates(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)

    assert should_log_cabinet_action('POST', '/cabinet/subscription/trial') is True
    assert should_log_cabinet_action('delete', '/cabinet/devices/1') is True
    # GET — не «кнопка», шум
    assert should_log_cabinet_action('GET', '/cabinet/subscription') is False
    # Админские действия уже пишутся в admin_audit_log
    assert should_log_cabinet_action('POST', '/cabinet/admin/users/1/balance') is False
    # Фоновый обмен токенов
    assert should_log_cabinet_action('POST', '/cabinet/auth/refresh') is False

    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', False, raising=False)
    assert should_log_cabinet_action('POST', '/cabinet/subscription/trial') is False


def test_schedule_skips_when_gated(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)
    with patch('app.services.user_action_log_service.asyncio.create_task') as create_task:
        schedule_cabinet_action_log(1, 'GET', '/cabinet/subscription')
        create_task.assert_not_called()

        schedule_cabinet_action_log(1, 'POST', '/cabinet/subscription/trial')
        create_task.assert_called_once()
        # Извлекаем корутину, чтобы не ругался warning про не-awaited
        create_task.call_args.args[0].close()


async def test_write_uses_stats_service(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)
    from app.services import user_action_log_service as mod

    log_call = AsyncMock()
    fake_db = AsyncMock()

    class FakeSessionCtx:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, *args):
            return False

    with (
        patch.object(mod, 'AsyncSessionLocal', lambda: FakeSessionCtx()),
        patch('app.services.menu_layout.service.MenuLayoutService.log_button_click', log_call),
    ):
        await mod._write_cabinet_action(42, 'POST', '/cabinet/subscriptions/5/renew')

    log_call.assert_awaited_once()
    kwargs = log_call.await_args.kwargs
    assert kwargs['button_id'] == 'POST /cabinet/subscriptions/{id}/renew'
    assert kwargs['user_id'] == 42
    assert kwargs['callback_data'] == '/cabinet/subscriptions/5/renew'
    assert kwargs['button_type'] == 'cabinet'
