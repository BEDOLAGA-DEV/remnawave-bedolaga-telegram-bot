"""Импорт из панели не подтягивает аккаунты, которых бот не создавал.

Репорт: в панели есть аккаунты, заведённые руками — без Telegram id и без
почты. Бот пишет личность в каждый свой аккаунт, поэтому аккаунт без неё — не
его: импорт в обоих режимах обязан пройти мимо, ничего не создать и сказать об
этом в логе, а не промолчать (раньше такие аккаунты исчезали из статистики
беззвучно, и оператор не мог понять, трогает их бот или нет).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog

import app.services.grace_access_runtime as grace_runtime_mod
from app.config import Settings
from app.services.remnawave_service import RemnaWaveService


def _panel_account(panel_id: int, *, username: str) -> SimpleNamespace:
    """Аккаунт, заведённый руками: имя есть, личности нет."""
    return SimpleNamespace(
        id=panel_id,
        short_uuid=f'short-{panel_id}',
        username=username,
        status=SimpleNamespace(value='ACTIVE'),
        telegram_id=None,
        email=None,
        expire_at=datetime.now(UTC) + timedelta(days=30),
        traffic_limit_bytes=0,
        used_traffic_bytes=0,
        hwid_device_limit=None,
        subscription_url='https://panel.example/sub',
        happ_crypto_link=None,
        active_internal_squads=[],
    )


def _empty_result() -> SimpleNamespace:
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list), scalar_one_or_none=lambda: None)


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_empty_result())
    return session


@pytest.fixture
def service(monkeypatch) -> RemnaWaveService:
    api = AsyncMock()
    roster = [_panel_account(101, username='user_555'), _panel_account(102, username='vip-client')]
    api.get_all_users_page_stream = AsyncMock(return_value={'users': roster, 'hasMore': False, 'nextCursor': None})

    svc = RemnaWaveService()
    svc._config_error = None

    @asynccontextmanager
    async def fake_client():
        yield api

    monkeypatch.setattr(svc, 'get_api_client', fake_client)
    monkeypatch.setattr(grace_runtime_mod, 'get_open_grace_subscription_ids', AsyncMock(return_value=set()))
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize('multi_tariff', [True, False], ids=['multi', 'single'])
async def test_import_skips_accounts_without_identity_and_says_so(monkeypatch, service, db, multi_tariff):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: multi_tariff)

    with structlog.testing.capture_logs() as logs:
        stats = await service.sync_users_from_panel(db, 'all')

    assert stats['created'] == 0
    assert stats['errors'] == 0
    skipped = [entry for entry in logs if 'созданы не ботом' in entry.get('event', '')]
    assert len(skipped) == 1, f'пропуск чужих аккаунтов должен быть назван в логе ровно раз: {logs}'
    assert skipped[0]['foreign_accounts_count'] == 2
