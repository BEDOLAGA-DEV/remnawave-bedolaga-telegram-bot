"""Единая фабрика клиента Redis.

redis-py ≥ 8 в режиме ``maint_notifications_config.enabled="auto"`` на каждом новом
соединении шлёт ``CLIENT MAINT_NOTIFICATIONS`` (фича Redis Enterprise); обычный Redis
команду не знает, и библиотека пишет в лог «Failed to enable maintenance
notifications» на каждое соединение. Фабрика выключает это явно, и все клиенты
бота обязаны создаваться через неё. conftest подменяет ``redis`` заглушкой, поэтому
проверяется контракт вызова, а не настоящий пул.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.utils import redis_client


APP_ROOT = Path(__file__).resolve().parents[2] / 'app'
FACTORY = APP_ROOT / 'utils' / 'redis_client.py'
CLIENT_CALL = re.compile(r'\b(?:from_url|Redis)\(')


class _Config:
    def __init__(self, enabled: bool | str = 'auto') -> None:
        self.enabled = enabled


@pytest.fixture
def from_url(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def fake(url: str, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(redis_client.redis, 'from_url', fake)
    monkeypatch.setattr(redis_client, 'MaintNotificationsConfig', _Config)
    return calls


def test_factory_disables_maintenance_notifications(from_url) -> None:
    redis_client.create_redis('redis://redis-host:6390/3')

    ((url, kwargs),) = from_url
    assert url == 'redis://redis-host:6390/3'
    assert kwargs['maint_notifications_config'].enabled is False


def test_factory_defaults_to_settings_url_and_keeps_explicit_kwargs(from_url, monkeypatch) -> None:
    monkeypatch.setattr(redis_client.settings, 'REDIS_URL', 'redis://from-settings:6379/0')

    redis_client.create_redis(decode_responses=True)

    ((url, kwargs),) = from_url
    assert url == 'redis://from-settings:6379/0'
    assert kwargs['decode_responses'] is True


def test_factory_skips_config_on_old_redis_py(from_url, monkeypatch) -> None:
    """redis-py без модуля maint_notifications: лишний kwarg уронил бы from_url."""
    monkeypatch.setattr(redis_client, 'MaintNotificationsConfig', None)

    redis_client.create_redis('redis://h:1/0')

    ((_url, kwargs),) = from_url
    assert 'maint_notifications_config' not in kwargs


def test_every_redis_client_in_app_goes_through_factory() -> None:
    """Сторож: прямой ``from_url``/``Redis(`` в app/ вернул бы шум и обошёл общие настройки."""
    offenders = []
    for path in APP_ROOT.rglob('*.py'):
        if path == FACTORY:
            continue
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith('#') or 'redis' not in line.lower():
                continue
            if CLIENT_CALL.search(line):
                offenders.append(f'{path.relative_to(APP_ROOT.parent)}:{number}: {stripped}')
    assert offenders == []
