"""Race in BackupService.start_auto_backup: concurrent restarts orphan loops.

At startup SystemSettingsService fires one start_auto_backup() task per
BACKUP_* key loaded from DB (7 keys in prod). start_auto_backup awaits the
old task's cancellation before writing the new one into _auto_backup_task,
so concurrent callers overwrite each other's pointer and the losers' loops
survive as untracked orphans — 6 parallel schedulers, 6 backups at 03:00.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services.backup_service import BackupService


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'BACKUP_LOCATION', str(tmp_path))
    svc = BackupService()
    svc._settings.auto_backup_enabled = True
    return svc


def _install_fake_loop(monkeypatch, svc: BackupService) -> list[asyncio.Task]:
    """Replace the real backup loop with a stub that tracks live tasks."""
    live: list[asyncio.Task] = []

    async def fake_loop(next_run=None):
        live.append(asyncio.current_task())
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            live.remove(asyncio.current_task())
            raise

    monkeypatch.setattr(svc, '_auto_backup_loop', fake_loop)
    return live


async def _settle():
    # даём осиротевшим таскам шанс стартовать/дообработать отмену
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_concurrent_starts_leave_exactly_one_loop(monkeypatch, service):
    live = _install_fake_loop(monkeypatch, service)

    # как в проде: 7 fire-and-forget рестартов из _apply_to_settings за один тик
    tasks = [asyncio.create_task(service.start_auto_backup()) for _ in range(7)]
    await asyncio.gather(*tasks)
    await _settle()

    try:
        assert len(live) == 1
    finally:
        for task in list(live):
            task.cancel()
        await asyncio.gather(*live, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_after_concurrent_starts_kills_all_loops(monkeypatch, service):
    live = _install_fake_loop(monkeypatch, service)

    tasks = [asyncio.create_task(service.start_auto_backup()) for _ in range(7)]
    await asyncio.gather(*tasks)
    await _settle()

    await service.stop_auto_backup()
    await _settle()

    try:
        assert live == []
    finally:
        for task in list(live):
            task.cancel()
        await asyncio.gather(*live, return_exceptions=True)
