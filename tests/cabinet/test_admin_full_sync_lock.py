"""Кабинет: второй запуск полной синхронизации, пока идёт первая, — 409, а не второй проход."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.services.remnawave_sync_service as sync_mod
from app.cabinet.routes import admin_remnawave


@pytest.mark.asyncio
async def test_full_sync_route_answers_409_while_another_run_is_in_progress(monkeypatch):
    monkeypatch.setattr(admin_remnawave, '_get_service', lambda: SimpleNamespace(is_configured=True))
    monkeypatch.setattr(sync_mod, 'perform_full_sync', AsyncMock(side_effect=sync_mod.FullSyncAlreadyRunning()))

    with pytest.raises(HTTPException) as exc:
        await admin_remnawave.sync_full(admin=SimpleNamespace(telegram_id=1), db=AsyncMock())

    assert exc.value.status_code == 409
    assert 'уже выполняется' in exc.value.detail
