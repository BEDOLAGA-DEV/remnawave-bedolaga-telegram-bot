from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_blocked_env_admin_still_reaches_the_bot(monkeypatch):
    """BLOCKED is set automatically when a user mutes the bot — it must not lock the owner out."""
    from app.middlewares import auth

    monkeypatch.setattr(type(auth.settings), 'get_admin_ids', lambda self: [42])
    monkeypatch.setattr(type(auth.settings), 'get_admin_emails', lambda self: [])

    blocked_admin = SimpleNamespace(status='blocked', telegram_id=42, email=None, email_verified=False)
    blocked_user = SimpleNamespace(status='blocked', telegram_id=43, email=None, email_verified=False)

    assert auth._is_blocked_non_admin(blocked_admin) is False
    assert auth._is_blocked_non_admin(blocked_user) is True


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
