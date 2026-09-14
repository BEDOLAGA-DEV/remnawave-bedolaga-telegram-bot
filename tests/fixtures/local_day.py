"""Помощники для тестов «календарного дня» в settings.TIMEZONE.

``get_local_timezone`` кэширован (``lru_cache``), поэтому подмена
``settings.TIMEZONE`` через monkeypatch сама по себе ничего не меняет — кэш
надо сбрасывать и при установке, и при откате.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.utils.timezone import get_local_timezone


def use_timezone(monkeypatch: pytest.MonkeyPatch, name: str) -> ZoneInfo:
    """Переключить settings.TIMEZONE на ``name`` и вернуть саму зону."""
    monkeypatch.setattr(settings, 'TIMEZONE', name, raising=False)
    get_local_timezone.cache_clear()
    return ZoneInfo(name)


def zone_where_local_date_differs_from_utc(now: datetime | None = None) -> str:
    """Зона, в которой прямо сейчас другая календарная дата, чем в UTC.

    Нужна тестам, которые должны падать на коде «сегодня = дата по UTC»
    независимо от того, в какое время суток идёт прогон: до полудня UTC
    берём UTC−12 (там ещё вчера), после — UTC+14 (там уже завтра).
    """
    moment = now or datetime.now(UTC)
    return 'Etc/GMT+12' if moment.hour < 12 else 'Pacific/Kiritimati'


@pytest.fixture
def reset_local_timezone_cache():
    """Сбросить кэш зоны после теста, чтобы подмена не утекла в соседей."""
    yield
    get_local_timezone.cache_clear()
