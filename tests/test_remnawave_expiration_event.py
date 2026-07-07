"""RemnaWave 2.8: единое событие user.expiration вместо четырёх expires_in_*/expired_ago.

meta.expiration — часы относительно истечения: отрицательные «до», положительные «после».
Хендлер должен раскладывать значение по существующим бакетам уведомлений.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _service(monkeypatch, calls: list[str]):
    from app.services.remnawave_webhook_service import RemnaWaveWebhookService

    svc = RemnaWaveWebhookService(bot=MagicMock())
    for name in (
        '_handle_expires_in_72h',
        '_handle_expires_in_48h',
        '_handle_expires_in_24h',
        '_handle_expired_24h_ago',
    ):

        async def _rec(db, user, subscription, data, _n=name):
            calls.append(_n)

        monkeypatch.setattr(svc, name, _rec)
    return svc


async def _fire(svc, hours):
    handler = svc._user_handlers['user.expiration']
    data = {'uuid': 'u-1'}
    if hours is not None:
        data['_meta'] = {'expiration': hours}
    await handler(None, SimpleNamespace(id=1), SimpleNamespace(id=2), data)


async def test_expiration_buckets(monkeypatch):
    cases = [
        (-72, '_handle_expires_in_72h'),
        (-100, '_handle_expires_in_72h'),
        (-48, '_handle_expires_in_48h'),
        (-60, '_handle_expires_in_48h'),
        (-24, '_handle_expires_in_24h'),
        (-1, '_handle_expires_in_24h'),
        (24, '_handle_expired_24h_ago'),
        (100, '_handle_expired_24h_ago'),
    ]
    for hours, expected in cases:
        calls: list[str] = []
        svc = _service(monkeypatch, calls)
        await _fire(svc, hours)
        assert calls == [expected], f'hours={hours}'


async def test_expiration_invalid_meta_is_noop(monkeypatch):
    calls: list[str] = []
    svc = _service(monkeypatch, calls)
    await _fire(svc, None)  # нет _meta вовсе
    await _fire(svc, 'garbage')
    assert calls == []
