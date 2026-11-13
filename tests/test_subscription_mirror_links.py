"""SUBSCRIPTION_MIRROR_LINKS: extra «Подключиться» buttons opening the same subscription via another domain."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.keyboards.inline as kb
from app.config import Settings, settings
from app.handlers.subscription import links
from app.localization.texts import get_texts
from app.utils.subscription_utils import build_subscription_mirror_links


SUB_URL = 'https://sub.example.com/AbCdEf123?lang=ru'
MIRROR = '🔁 Резерв|https://mirror.example.net'
TWO_MIRRORS = 'Резерв 1|https://mirror.example.net;Резерв 2|https://mirror2.example.org'


def _set_mirrors(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, 'SUBSCRIPTION_MIRROR_LINKS', value)


# --- parsing ---------------------------------------------------------------


def test_empty_setting_gives_no_mirrors(monkeypatch):
    _set_mirrors(monkeypatch, '')
    assert settings.get_subscription_mirror_links() == []


def test_parses_multiple_entries_and_normalizes_origin(monkeypatch):
    _set_mirrors(
        monkeypatch,
        '🔁 Первый|Mirror.Example.net/ignored/path ; Второй|http://m2.example.org:8443\nТретий|m3.example.io',
    )
    assert settings.get_subscription_mirror_links() == [
        ('🔁 Первый', 'https://mirror.example.net'),
        ('Второй', 'http://m2.example.org:8443'),
        ('Третий', 'https://m3.example.io'),
    ]


@pytest.mark.parametrize(
    'value',
    [
        'no-separator.example.com',
        '|https://mirror.example.net',
        'Без адреса|',
        'FTP|ftp://mirror.example.net',
        'Схема без хоста|https://',
    ],
)
def test_invalid_entries_are_skipped(monkeypatch, value):
    _set_mirrors(monkeypatch, f'{value};{MIRROR}')
    assert settings.get_subscription_mirror_links() == [('🔁 Резерв', 'https://mirror.example.net')]


# --- link building -----------------------------------------------------------


def test_mirror_keeps_path_and_query(monkeypatch):
    _set_mirrors(monkeypatch, MIRROR)
    assert build_subscription_mirror_links(SUB_URL) == [
        ('🔁 Резерв', 'https://mirror.example.net/AbCdEf123?lang=ru'),
    ]


def test_mirror_on_same_host_is_skipped(monkeypatch):
    _set_mirrors(monkeypatch, f'Тот же|https://SUB.example.com;{MIRROR}')
    assert [label for label, _ in build_subscription_mirror_links(SUB_URL)] == ['🔁 Резерв']


@pytest.mark.parametrize('link', [None, '', 'happ://crypt4/payload', 'not a url'])
def test_non_http_links_get_no_mirrors(monkeypatch, link):
    _set_mirrors(monkeypatch, MIRROR)
    assert build_subscription_mirror_links(link) == []


# --- keyboard rows -----------------------------------------------------------


def test_miniapp_mode_gives_web_app_buttons(monkeypatch):
    _set_mirrors(monkeypatch, MIRROR)
    rows = kb.get_subscription_mirror_link_rows(SUB_URL, 'miniapp_subscription')
    assert len(rows) == 1
    button = rows[0][0]
    assert button.text == '🔁 Резерв'
    assert button.web_app.url == 'https://mirror.example.net/AbCdEf123?lang=ru'
    assert button.url is None


def test_link_mode_gives_url_buttons(monkeypatch):
    _set_mirrors(monkeypatch, MIRROR)
    rows = kb.get_subscription_mirror_link_rows(SUB_URL, 'link')
    assert rows[0][0].url == 'https://mirror.example.net/AbCdEf123?lang=ru'
    assert rows[0][0].web_app is None


@pytest.mark.parametrize('mode', ['guide', 'miniapp_custom', 'happ_cryptolink'])
def test_other_modes_get_no_mirror_buttons(monkeypatch, mode):
    _set_mirrors(monkeypatch, MIRROR)
    assert kb.get_subscription_mirror_link_rows(SUB_URL, mode) == []
    assert kb.get_subscription_mirror_rows(SUB_URL, mode) == []


def test_mode_defaults_to_setting(monkeypatch):
    _set_mirrors(monkeypatch, MIRROR)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link')
    assert kb.get_subscription_mirror_link_rows(SUB_URL)[0][0].url.startswith('https://mirror.example.net/')


# --- real keyboards ----------------------------------------------------------


def _fake_sub() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        actual_status='active',
        status='active',
        tariff_id=None,
        tariff=None,
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
        is_trial=False,
        subscription_url=SUB_URL,
        subscription_crypto_link=None,
    )


def _button_rows(markup) -> list[list]:
    return [list(row) for row in markup.inline_keyboard]


def _connect_and_next(markup):
    rows = _button_rows(markup)
    index = next(i for i, row in enumerate(rows) if row[0].web_app and row[0].web_app.url == SUB_URL)
    return rows[index][0], rows[index + 1][0]


@pytest.fixture
def miniapp_mode(monkeypatch):
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'miniapp_subscription')
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(Settings, 'is_cabinet_mode', lambda self: False)


def test_main_menu_puts_backup_button_right_under_connect(monkeypatch, miniapp_mode):
    _set_mirrors(monkeypatch, MIRROR)
    markup = kb.get_main_menu_keyboard(
        'ru', has_active_subscription=True, subscription_is_active=True, subscription=_fake_sub()
    )
    _, backup = _connect_and_next(markup)
    # One mirror: the button keeps the generic text and opens the mirror directly.
    assert backup.text == '🔁 Резервное подключение'
    assert backup.web_app.url == 'https://mirror.example.net/AbCdEf123?lang=ru'
    assert backup.callback_data is None


def test_subscription_screen_puts_mirror_right_under_connect(monkeypatch, miniapp_mode):
    _set_mirrors(monkeypatch, MIRROR)
    markup = kb.get_subscription_keyboard('ru', has_subscription=True, subscription=_fake_sub())
    _, backup = _connect_and_next(markup)
    assert backup.web_app.url == 'https://mirror.example.net/AbCdEf123?lang=ru'


def test_keyboards_unchanged_without_setting(monkeypatch, miniapp_mode):
    _set_mirrors(monkeypatch, '')
    markup = kb.get_main_menu_keyboard(
        'ru', has_active_subscription=True, subscription_is_active=True, subscription=_fake_sub()
    )
    urls = [btn.web_app.url for row in markup.inline_keyboard for btn in row if btn.web_app]
    assert urls == [SUB_URL]


# --- backup menu --------------------------------------------------------------


def test_several_mirrors_collapse_into_menu_button(monkeypatch):
    _set_mirrors(monkeypatch, TWO_MIRRORS)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    rows = kb.get_subscription_mirror_rows(SUB_URL, 'miniapp_subscription', texts=get_texts('ru'), subscription_id=5)
    assert len(rows) == 1
    button = rows[0][0]
    assert button.text == '🔁 Резервное подключение'
    assert button.callback_data == 'subscription_mirrors'
    assert button.web_app is None


def test_menu_button_carries_subscription_id_in_multi_tariff(monkeypatch):
    _set_mirrors(monkeypatch, TWO_MIRRORS)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    rows = kb.get_subscription_mirror_rows(SUB_URL, 'link', texts=get_texts('en'), subscription_id=5)
    assert rows[0][0].callback_data == 'subscription_mirrors:5'
    assert rows[0][0].text == '🔁 Backup connection'


def test_link_rows_list_every_mirror(monkeypatch):
    _set_mirrors(monkeypatch, TWO_MIRRORS)
    rows = kb.get_subscription_mirror_link_rows(SUB_URL, 'miniapp_subscription')
    assert [(row[0].text, row[0].web_app.url) for row in rows] == [
        ('Резерв 1', 'https://mirror.example.net/AbCdEf123?lang=ru'),
        ('Резерв 2', 'https://mirror2.example.org/AbCdEf123?lang=ru'),
    ]


def test_mirror_matching_main_host_is_left_out_of_menu(monkeypatch):
    _set_mirrors(monkeypatch, 'Резерв 1|https://mirror.example.net;Резерв 2|https://sub.example.com')
    rows = kb.get_subscription_mirror_link_rows(SUB_URL, 'miniapp_subscription')
    assert [(row[0].text, row[0].web_app.url) for row in rows] == [
        ('Резерв 1', 'https://mirror.example.net/AbCdEf123?lang=ru'),
    ]


def test_only_mirror_on_main_host_gives_no_backup_button(monkeypatch):
    _set_mirrors(monkeypatch, 'Резерв|https://sub.example.com')
    assert kb.get_subscription_mirror_rows(SUB_URL, 'miniapp_subscription', texts=get_texts('ru')) == []


def test_main_menu_shows_menu_button_for_several_mirrors(monkeypatch, miniapp_mode):
    _set_mirrors(monkeypatch, TWO_MIRRORS)
    markup = kb.get_main_menu_keyboard(
        'ru', has_active_subscription=True, subscription_is_active=True, subscription=_fake_sub()
    )
    _, mirror = _connect_and_next(markup)
    assert mirror.callback_data == 'subscription_mirrors'


def _mirrors_callback(data: str = 'subscription_mirrors'):
    return SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))


@pytest.mark.asyncio
async def test_mirrors_menu_lists_mirrors_and_back(monkeypatch):
    _set_mirrors(monkeypatch, TWO_MIRRORS)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'miniapp_subscription')
    monkeypatch.setattr(links, '_resolve_subscription', AsyncMock(return_value=(_fake_sub(), 1)))
    callback = _mirrors_callback()

    await links.handle_subscription_mirrors(callback, SimpleNamespace(id=1, language='ru'), AsyncMock())

    kwargs = callback.message.edit_text.await_args.kwargs
    rows = kwargs['reply_markup'].inline_keyboard
    assert [row[0].web_app.url for row in rows[:2]] == [
        'https://mirror.example.net/AbCdEf123?lang=ru',
        'https://mirror2.example.org/AbCdEf123?lang=ru',
    ]
    assert rows[2][0].callback_data == 'back_to_menu'
    assert 'Резервное подключение' in callback.message.edit_text.await_args.args[0]
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_mirrors_menu_without_mirrors_shows_alert(monkeypatch):
    _set_mirrors(monkeypatch, '')
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'miniapp_subscription')
    monkeypatch.setattr(links, '_resolve_subscription', AsyncMock(return_value=(_fake_sub(), 1)))
    callback = _mirrors_callback()

    await links.handle_subscription_mirrors(callback, SimpleNamespace(id=1, language='ru'), AsyncMock())

    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


def test_single_mirror_button_opens_it_directly_in_link_mode(monkeypatch):
    _set_mirrors(monkeypatch, MIRROR)
    rows = kb.get_subscription_mirror_rows(SUB_URL, 'link', texts=get_texts('en'))
    assert len(rows) == 1
    assert rows[0][0].text == '🔁 Backup connection'
    assert rows[0][0].url == 'https://mirror.example.net/AbCdEf123?lang=ru'


def test_mirror_on_main_host_leaves_single_direct_button(monkeypatch):
    """Резерв на основном домене скрыт: остаётся одно зеркало, кнопка открывает его сразу, без меню."""
    _set_mirrors(monkeypatch, 'Резерв 1|https://mirror.example.net;Резерв 2|https://sub.example.com')
    rows = kb.get_subscription_mirror_rows(SUB_URL, 'miniapp_subscription', texts=get_texts('ru'))
    assert [(row[0].text, row[0].web_app.url) for row in rows] == [
        ('🔁 Резервное подключение', 'https://mirror.example.net/AbCdEf123?lang=ru'),
    ]
