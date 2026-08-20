from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_blocked_admin_is_not_stopped_before_registration_gate(monkeypatch):
    from app.middlewares import auth

    monkeypatch.setattr(type(auth.settings), 'is_admin', lambda self, telegram_id: telegram_id == 42)
    blocked = SimpleNamespace(status='blocked', telegram_id=42)
    ordinary = SimpleNamespace(status='blocked', telegram_id=43)

    assert auth._is_blocked_non_admin(blocked) is False
    assert auth._is_blocked_non_admin(ordinary) is True


@pytest.mark.asyncio
async def test_refresh_remnawave_description_uses_numeric_panel_id(monkeypatch):
    from app.middlewares import auth

    api = SimpleNamespace(update_user=AsyncMock())

    class _ApiContext:
        async def __aenter__(self):
            return api

        async def __aexit__(self, exc_type, exc, tb):
            return False

    service = SimpleNamespace(get_api_client=lambda: _ApiContext())
    monkeypatch.setattr(auth, 'RemnaWaveService', lambda: service)

    await auth._refresh_remnawave_description(4242, 'updated description', 99)

    api.update_user.assert_awaited_once_with(user_id=4242, description='updated description')
