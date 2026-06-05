"""Regression tests for BackupService.get_backup_list() corruption handling.

Reported by user 2026-05-15 07:20: empty .tar.gz file in backups directory
caused `tarfile.ReadError: empty file` propagating through `get_backup_list`,
logged as ERROR (which TelegramNotifierProcessor forwards to the admin chat
on every list invocation).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.backup_service import BackupService


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    """Isolated backup directory under pytest's tmp_path."""
    d = tmp_path / 'backups'
    d.mkdir()
    return d


@pytest.fixture
def service(backup_dir: Path) -> BackupService:
    """A minimally-mocked BackupService bound to a temp backup dir."""
    svc = BackupService.__new__(BackupService)
    svc.backup_dir = backup_dir
    # Telegram-bot/db dependencies aren't used by get_backup_list. Stub for safety.
    svc.bot = AsyncMock()
    return svc


def _write_valid_archive(path: Path, metadata: dict) -> None:
    """Create a tar.gz with a metadata.json member at the root."""
    payload = json.dumps(metadata).encode('utf-8')
    with tarfile.open(path, 'w:gz') as tar:
        info = tarfile.TarInfo(name='metadata.json')
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))


@pytest.mark.asyncio
async def test_get_backup_list_skips_empty_tar_gz(service: BackupService, backup_dir: Path) -> None:
    """Empty .tar.gz must not raise — it's marked corrupted and listing continues."""
    empty = backup_dir / 'backup_20260515_071955.tar.gz'
    empty.write_bytes(b'')  # 0 bytes — exactly the bug scenario

    result = await service.get_backup_list()

    assert len(result) == 1
    entry = result[0]
    assert entry['filename'] == empty.name
    assert entry['corrupted'] is True
    assert entry['file_size_bytes'] == 0
    assert 'Файл пуст' in entry['error']


@pytest.mark.asyncio
async def test_get_backup_list_recovers_other_files_when_one_is_empty(service: BackupService, backup_dir: Path) -> None:
    """One bad file doesn't poison the rest of the listing."""
    empty = backup_dir / 'backup_20260101_000000.tar.gz'
    empty.write_bytes(b'')

    good = backup_dir / 'backup_20260515_120000.tar.gz'
    _write_valid_archive(
        good,
        metadata={
            'timestamp': '2026-05-15T12:00:00+00:00',
            'tables_count': 42,
            'total_records': 1337,
            'created_by': 'cron',
            'database_type': 'postgresql',
            'format_version': '2.0',
        },
    )

    result = await service.get_backup_list()
    by_name = {entry['filename']: entry for entry in result}

    assert empty.name in by_name
    assert by_name[empty.name]['corrupted'] is True

    assert good.name in by_name
    good_entry = by_name[good.name]
    assert good_entry.get('corrupted') is not True
    assert good_entry['tables_count'] == 42
    assert good_entry['total_records'] == 1337
    assert good_entry['database_type'] == 'postgresql'


@pytest.mark.asyncio
async def test_get_backup_list_handles_truncated_gzip(service: BackupService, backup_dir: Path) -> None:
    """Truncated/corrupted gzip → caught as known-corruption, not as bare Exception."""
    bad = backup_dir / 'backup_truncated.tar.gz'
    bad.write_bytes(b'\x1f\x8b\x08\x00')  # gzip magic but no real payload

    result = await service.get_backup_list()

    assert len(result) == 1
    assert result[0]['corrupted'] is True
    # Не падает; помечен с типом исключения.
    assert result[0]['filename'] == bad.name


@pytest.mark.asyncio
async def test_get_backup_list_handles_garbage_json(service: BackupService, backup_dir: Path) -> None:
    """Non-archive JSON backup with garbage content → corrupted entry, not crash."""
    bad = backup_dir / 'backup_legacy.json'
    bad.write_text('{not valid json,,,', encoding='utf-8')

    result = await service.get_backup_list()

    assert len(result) == 1
    assert result[0]['corrupted'] is True


@pytest.mark.asyncio
async def test_get_backup_list_corrupted_entries_do_not_log_as_error(
    service: BackupService, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known corruption logs as warning, not error — TelegramNotifierProcessor
    skips warnings, so the admin chat won't be spammed on every list call."""
    levels: list[str] = []

    real_logger = MagicMock()
    real_logger.warning.side_effect = lambda *a, **kw: levels.append('warning')
    real_logger.error.side_effect = lambda *a, **kw: levels.append('error')
    real_logger.info.side_effect = lambda *a, **kw: levels.append('info')
    real_logger.debug.side_effect = lambda *a, **kw: levels.append('debug')

    monkeypatch.setattr('app.services.backup_service.logger', real_logger)

    (backup_dir / 'backup_empty.tar.gz').write_bytes(b'')
    (backup_dir / 'backup_bad.json').write_text('{not json', encoding='utf-8')
    bad_gzip = backup_dir / 'backup_truncated.tar.gz'
    bad_gzip.write_bytes(b'\x1f\x8b\x08\x00')

    await service.get_backup_list()

    # Все три случая — known corruption: warning'и, нулевая утечка error.
    assert 'error' not in levels, f'expected no error logs, got {levels}'
    assert levels.count('warning') >= 3
