"""Адрес логотипа в rich-сообщениях меняется вместе с файлом.

Telegram кэширует картинку по адресу, а не по содержимому. Автоадрес
``/cabinet/branding/bot-logo`` был одним и тем же всегда — оператор менял файл
логотипа, а бот в rich-меню и уведомлениях показывал старый, пока кто-то не
дописывал ``?v=2`` руками. Теперь в адрес входит отпечаток файла: новый файл —
новый адрес — Telegram скачивает заново. Явный MAIN_MENU_RICH_LOGO_URL — как задал
оператор, без дописок.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

from app.config import settings
from app.utils import rich_menu


@pytest.fixture
def logo(monkeypatch, tmp_path):
    path = tmp_path / 'logo.png'
    path.write_bytes(b'png-v1')
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', '', raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example.com/webhook', raising=False)
    monkeypatch.setattr(settings, 'LOGO_FILE', str(path), raising=False)
    rich_menu._reset_rich_menu_availability()
    return path


def _version(url: str) -> str:
    parsed = urlparse(url)
    assert f'{parsed.scheme}://{parsed.netloc}{parsed.path}' == 'https://bot.example.com/cabinet/branding/bot-logo'
    return parse_qs(parsed.query)['v'][0]


def test_auto_url_carries_the_file_version(logo):
    version = _version(rich_menu._resolve_rich_logo_url())

    assert version and version.isalnum()
    assert _version(rich_menu._resolve_rich_logo_url()) == version, 'файл не менялся — адрес тот же'


def test_new_logo_file_gets_a_new_address(logo):
    before = _version(rich_menu._resolve_rich_logo_url())

    logo.write_bytes(b'png-v2-longer')
    stat = logo.stat()
    os.utime(logo, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert _version(rich_menu._resolve_rich_logo_url()) != before


def test_explicit_url_is_left_exactly_as_the_operator_wrote_it(logo, monkeypatch):
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_LOGO_URL', 'https://cdn.example.com/logo.png', raising=False)

    assert rich_menu._resolve_rich_logo_url() == 'https://cdn.example.com/logo.png'
