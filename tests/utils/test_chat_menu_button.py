from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.utils.chat_menu_button import configure_chat_menu_button


def _fake_settings(**overrides):
    base = {
        'MENU_BUTTON_WEBAPP_ENABLED': True,
        'MENU_BUTTON_WEBAPP_URL': 'https://cab.example.com',
        'MENU_BUTTON_WEBAPP_TEXT': 'Кабинет',
        'MINIAPP_CUSTOM_URL': '',
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_disabled_does_not_touch_menu_button(monkeypatch):
    monkeypatch.setattr('app.utils.chat_menu_button.settings', _fake_settings(MENU_BUTTON_WEBAPP_ENABLED=False))
    bot = AsyncMock()
    assert await configure_chat_menu_button(bot) is False
    bot.set_chat_menu_button.assert_not_awaited()


async def test_enabled_sets_webapp_menu_button(monkeypatch):
    from aiogram.types import MenuButtonWebApp

    monkeypatch.setattr('app.utils.chat_menu_button.settings', _fake_settings())
    bot = AsyncMock()

    assert await configure_chat_menu_button(bot) is True
    bot.set_chat_menu_button.assert_awaited_once()
    button = bot.set_chat_menu_button.await_args.kwargs['menu_button']
    assert isinstance(button, MenuButtonWebApp)
    assert button.web_app.url == 'https://cab.example.com'
    assert button.text == 'Кабинет'


async def test_falls_back_to_miniapp_custom_url(monkeypatch):
    monkeypatch.setattr(
        'app.utils.chat_menu_button.settings',
        _fake_settings(MENU_BUTTON_WEBAPP_URL='', MINIAPP_CUSTOM_URL='https://miniapp.example.com'),
    )
    bot = AsyncMock()

    assert await configure_chat_menu_button(bot) is True
    button = bot.set_chat_menu_button.await_args.kwargs['menu_button']
    assert button.web_app.url == 'https://miniapp.example.com'


async def test_non_https_url_is_skipped(monkeypatch):
    monkeypatch.setattr(
        'app.utils.chat_menu_button.settings',
        _fake_settings(MENU_BUTTON_WEBAPP_URL='http://insecure.example', MINIAPP_CUSTOM_URL=''),
    )
    bot = AsyncMock()

    assert await configure_chat_menu_button(bot) is False
    bot.set_chat_menu_button.assert_not_awaited()


async def test_empty_text_defaults(monkeypatch):
    monkeypatch.setattr('app.utils.chat_menu_button.settings', _fake_settings(MENU_BUTTON_WEBAPP_TEXT='  '))
    bot = AsyncMock()

    assert await configure_chat_menu_button(bot) is True
    button = bot.set_chat_menu_button.await_args.kwargs['menu_button']
    assert button.text == 'Кабинет'
