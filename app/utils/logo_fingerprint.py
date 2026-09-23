"""Отпечаток файла логотипа — версия для адресов и кэшей.

Telegram кэширует картинку по адресу, а file_id живёт, пока его помнят.
Оператор меняет файл логотипа, а бот продолжал показывать старый: адрес
``/cabinet/branding/bot-logo`` не менялся никогда, file_id — до рестарта.
Отпечаток (расширение, mtime, размер) меняется при каждой замене файла и
дешёв: один ``stat`` без чтения содержимого.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def logo_fingerprint(logo_path: Path | None) -> str:
    """Отпечаток файла логотипа: меняется при каждой загрузке нового файла."""
    if logo_path is None:
        return 'none'
    try:
        stat = logo_path.stat()
    except OSError:
        return 'missing'
    return f'{logo_path.suffix.lower()}:{stat.st_mtime_ns}:{stat.st_size}'


def logo_version(logo_path: Path | None) -> str | None:
    """Короткая версия для ``?v=`` в адресе; ``None`` — файла нет."""
    fingerprint = logo_fingerprint(logo_path)
    if fingerprint in ('none', 'missing'):
        return None
    return hashlib.sha1(fingerprint.encode()).hexdigest()[:10]
