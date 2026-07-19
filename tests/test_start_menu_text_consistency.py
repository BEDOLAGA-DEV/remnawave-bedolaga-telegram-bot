"""Regression: /start must render the main menu via the SAME builder as the
"back to menu" navigation, so the subscription block (including the
multi-tariff "🟢 <tariff> — до …" format) is identical.

Previously app/handlers/start.py had its own stale formatter, so /start
showed the legacy "💎 Активна" status until the user navigated away and back.
"""

import pytest


@pytest.mark.asyncio
async def test_start_main_menu_text_delegates_to_menu_builder(monkeypatch):
    from app.handlers import menu as menu_mod, start as start_mod

    seen = {}

    async def fake_builder(user, texts, db):
        seen['args'] = (user, texts, db)
        return 'CANONICAL_MENU_TEXT'

    monkeypatch.setattr(menu_mod, 'get_main_menu_text', fake_builder)

    user, texts, db = object(), object(), object()
    result = await start_mod.get_main_menu_text(user, texts, db)

    assert result == 'CANONICAL_MENU_TEXT'
    assert seen['args'] == (user, texts, db)


def test_start_no_longer_has_duplicate_status_formatter():
    """The duplicate formatter that caused the /start-vs-menu divergence is gone."""
    from app.handlers import start as start_mod

    assert not hasattr(start_mod, '_get_subscription_status')
