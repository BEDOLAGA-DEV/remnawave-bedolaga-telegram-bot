"""Кэш file_id логотипа сбрасывается, когда файл логотипа заменили.

Обычные (не rich) сообщения шлют логотип файлом и запоминают выданный
Telegram file_id, чтобы не грузить картинку каждый раз. Кэш жил до рестарта:
оператор заменял файл, а бот продолжал слать старую картинку по старому
file_id. Теперь кэш привязан к отпечатку файла (mtime + размер).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from aiogram.types import FSInputFile

from app.utils import message_patch


@pytest.fixture
def logo(monkeypatch, tmp_path):
    path = tmp_path / 'logo.png'
    path.write_bytes(b'png-v1')
    monkeypatch.setattr(message_patch, 'LOGO_PATH', path)
    monkeypatch.setattr(message_patch, '_logo_path_valid', True)
    monkeypatch.setattr(message_patch, '_logo_file_id', None)
    monkeypatch.setattr(message_patch, '_logo_send_path', None)
    monkeypatch.setattr(message_patch, '_prepare_logo_for_send', lambda p: p)
    return path


def _telegram_answer(file_id: str) -> SimpleNamespace:
    return SimpleNamespace(photo=[SimpleNamespace(file_id='thumb'), SimpleNamespace(file_id=file_id)])


def _replace_file(path) -> None:
    path.write_bytes(b'png-v2-longer')
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_cached_file_id_is_reused_while_the_file_is_the_same(logo):
    assert isinstance(message_patch.get_logo_media(), FSInputFile)
    message_patch._cache_logo_file_id(_telegram_answer('old-file-id'))

    assert message_patch.get_logo_media() == 'old-file-id'


def test_replaced_logo_file_drops_the_cached_file_id(logo):
    message_patch._cache_logo_file_id(_telegram_answer('old-file-id'))
    assert message_patch.get_logo_media() == 'old-file-id'

    _replace_file(logo)

    media = message_patch.get_logo_media()
    assert isinstance(media, FSInputFile), 'после замены файла логотип грузится заново'
    message_patch._cache_logo_file_id(_telegram_answer('new-file-id'))
    assert message_patch.get_logo_media() == 'new-file-id'
