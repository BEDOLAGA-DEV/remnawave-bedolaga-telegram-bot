"""Tests for the connect-button routing through the HAPP/INCY app-choice step.

The top-level "Подключиться" entry buttons in both ``get_main_menu_keyboard``
(Site A) and ``get_subscription_keyboard`` (Site B) must route through the
app-choice callback ``nz!_subscription_connect`` for every link-based connect
mode, instead of opening the subscription link directly (web_app/url) or going
to ``nz!_open_subscription_link``. The only exception is ``miniapp_custom``,
which has no per-user link to encrypt and keeps its direct web_app button.
"""

from types import SimpleNamespace

import pytest

import app.keyboards.inline as inline
from app.config import settings


CONNECT_TEXT_FALLBACK = '🔗 Подключиться'


def _make_subscription():
    """A subscription object that yields a non-None display link in any mode."""
    return SimpleNamespace(
        id=42,
        subscription_url='https://panel.example/sub/token',
        subscription_crypto_link='happ://crypt5/abc',
        is_trial=False,
        traffic_limit_gb=0,
        tariff_id=None,
        tariff=None,
        status='active',
        is_daily_paused=False,
    )


def _find_connect_button(markup):
    """Return the first button whose text is the CONNECT_BUTTON value."""
    connect_text = inline.get_texts('ru').t('CONNECT_BUTTON', CONNECT_TEXT_FALLBACK)
    for row in markup.inline_keyboard:
        for button in row:
            text = button.text or ''
            if text == connect_text or 'Подключиться' in text:
                return button
    return None


@pytest.fixture
def base_settings(monkeypatch):
    """Force a simple, non-cabinet, single-tariff environment.

    ``settings`` is a frozen-ish pydantic model that rejects assignment to
    names that are not declared fields, so we patch the underlying fields the
    relevant helper methods read rather than the methods themselves:
      * ``MAIN_MENU_MODE`` -> ``get_main_menu_mode`` / ``is_cabinet_mode``
      * ``MULTI_TARIFF_ENABLED`` -> ``is_multi_tariff_enabled`` (no ':id' suffix)
    """
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://miniapp.example/custom')
    # Non-cabinet so get_main_menu_keyboard renders the inline keyboard path.
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    # Single tariff -> no ':id' suffix on callbacks, keeps assertions simple.
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    return settings


# --- Site A: get_main_menu_keyboard ----------------------------------------


@pytest.mark.parametrize('connect_mode', ['happ_cryptolink', 'link', 'miniapp_subscription'])
def test_main_menu_connect_routes_through_app_choice(base_settings, monkeypatch, connect_mode):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', connect_mode, raising=False)

    markup = inline.get_main_menu_keyboard(
        language='ru',
        has_active_subscription=True,
        subscription_is_active=True,
        subscription=_make_subscription(),
    )

    button = _find_connect_button(markup)
    assert button is not None, f'connect button missing for mode={connect_mode}'
    assert button.callback_data == 'nz!_subscription_connect', (
        f'mode={connect_mode}: expected app-choice callback, got '
        f'callback={button.callback_data!r} url={button.url!r} web_app={button.web_app!r}'
    )
    assert button.web_app is None, f'mode={connect_mode}: must not be a direct web_app button'
    assert button.url is None, f'mode={connect_mode}: must not be a direct url button'


def test_main_menu_miniapp_custom_keeps_webapp(base_settings, monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'miniapp_custom', raising=False)

    markup = inline.get_main_menu_keyboard(
        language='ru',
        has_active_subscription=True,
        subscription_is_active=True,
        subscription=_make_subscription(),
    )

    button = _find_connect_button(markup)
    assert button is not None
    assert button.web_app is not None, 'miniapp_custom must remain a direct web_app button'
    assert button.web_app.url == 'https://miniapp.example/custom'
    assert button.callback_data is None


# --- Site B: get_subscription_keyboard -------------------------------------


@pytest.mark.parametrize('connect_mode', ['happ_cryptolink', 'link', 'miniapp_subscription'])
def test_subscription_keyboard_connect_routes_through_app_choice(base_settings, monkeypatch, connect_mode):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', connect_mode, raising=False)

    markup = inline.get_subscription_keyboard(
        language='ru',
        has_subscription=True,
        is_trial=False,
        subscription=_make_subscription(),
    )

    button = _find_connect_button(markup)
    assert button is not None, f'connect button missing for mode={connect_mode}'
    assert button.callback_data is not None, (
        f'mode={connect_mode}: must be a callback button, got '
        f'url={button.url!r} web_app={button.web_app!r}'
    )
    assert button.callback_data.startswith('nz!_subscription_connect'), (
        f'mode={connect_mode}: expected app-choice callback, got {button.callback_data!r}'
    )
    assert button.web_app is None
    assert button.url is None


def test_subscription_keyboard_miniapp_custom_keeps_webapp(base_settings, monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'miniapp_custom', raising=False)

    markup = inline.get_subscription_keyboard(
        language='ru',
        has_subscription=True,
        is_trial=False,
        subscription=_make_subscription(),
    )

    button = _find_connect_button(markup)
    assert button is not None
    assert button.web_app is not None, 'miniapp_custom must remain a direct web_app button'
    assert button.web_app.url == 'https://miniapp.example/custom'
    assert button.callback_data is None
