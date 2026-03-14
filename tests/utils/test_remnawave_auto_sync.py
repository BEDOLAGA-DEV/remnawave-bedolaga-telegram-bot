import asyncio
from collections import deque
from datetime import UTC, datetime, time as time_cls
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.remnawave_service import RemnaWaveConfigurationError
from app.services.remnawave_sync_service import RemnaWaveAutoSyncService


@pytest.mark.parametrize(
    'raw, expected',
    [
        ('03:00, 15:30 03:00; 07:05', [time_cls(3, 0), time_cls(7, 5), time_cls(15, 30)]),
        ('', []),
        (None, []),
        ('25:00, 10:70, test, 09:15', [time_cls(9, 15)]),
    ],
)
def test_parse_daily_time_list(raw, expected):
    assert settings.parse_daily_time_list(raw) == expected


def _patch_datetime(monkeypatch, current):
    real_datetime = datetime

    monkeypatch.setattr(
        'app.services.remnawave_sync_service.datetime',
        SimpleNamespace(
            now=lambda tz=None: current,
            combine=lambda date_obj, time_obj, tzinfo=None: real_datetime.combine(date_obj, time_obj, tzinfo=tzinfo),
        ),
    )


def test_calculate_next_run_same_day(monkeypatch):
    service = RemnaWaveAutoSyncService()
    current = datetime(2024, 1, 1, 2, 30, tzinfo=UTC)
    _patch_datetime(monkeypatch, current)

    next_run = service._calculate_next_run([time_cls(1, 0), time_cls(3, 0)])

    assert next_run == datetime(2024, 1, 1, 3, 0, tzinfo=UTC)


def test_calculate_next_run_rollover(monkeypatch):
    service = RemnaWaveAutoSyncService()
    current = datetime(2024, 1, 1, 23, 45, tzinfo=UTC)
    _patch_datetime(monkeypatch, current)

    next_run = service._calculate_next_run([time_cls(1, 0), time_cls(10, 0)])

    assert next_run == datetime(2024, 1, 2, 1, 0, tzinfo=UTC)


def test_perform_sync_rebuilds_service_on_each_run(monkeypatch):
    class StubService:
        def __init__(self, *, configured: bool, user_stats=None, squads=None):
            self.is_configured = configured
            self.configuration_error = None if configured else 'missing config'
            self._user_stats = user_stats or {'synced': 1}
            self._squads = squads or []
            self.sync_calls = 0
            self.squad_calls = 0

        async def sync_users_from_panel(self, session, scope):
            self.sync_calls += 1
            return self._user_stats

        async def get_all_squads(self):
            self.squad_calls += 1
            return self._squads

    services = deque(
        [
            StubService(configured=True),  # used during service __init__
            StubService(configured=False),
            StubService(
                configured=True,
                user_stats={'synced': 2},
                squads=[{'id': 1}, {'id': 2}],
            ),
        ]
    )

    def factory():
        return services.popleft()

    async def fake_sync_with_remnawave(session, squads):
        return 1, 2, 3

    cache_mock = SimpleNamespace(delete_pattern=AsyncMock())

    class DummySession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        'app.services.remnawave_sync_service.AsyncSessionLocal',
        DummySession,
    )
    monkeypatch.setattr(
        'app.services.remnawave_sync_service.sync_with_remnawave',
        fake_sync_with_remnawave,
    )
    monkeypatch.setattr(
        'app.services.remnawave_sync_service.cache',
        cache_mock,
    )

    async def runner():
        service = RemnaWaveAutoSyncService(service_factory=factory)

        with pytest.raises(RemnaWaveConfigurationError):
            await service._perform_sync()

        user_stats, server_stats = await service._perform_sync()

        assert user_stats == {'synced': 2}
        assert server_stats == {'created': 1, 'updated': 2, 'removed': 3, 'total': 2}

    asyncio.run(runner())

    assert not services
    cache_mock.delete_pattern.assert_awaited_once_with('available_countries*')
