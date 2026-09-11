"""Массовый проход «в панель»: без сброса устройств и без спама в админ-чат на троттлинг."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.external.remnawave_api import RemnaWaveTransientError
from app.services.panel_sync import runner


def _subscription(sub_id: int):
    return SimpleNamespace(id=sub_id, user=SimpleNamespace(id=sub_id * 10, telegram_id=None))


@pytest.fixture
def _batches(monkeypatch):
    subs = [_subscription(1), _subscription(2)]
    calls = {'offset': []}

    async def fake_batch(db, offset=0, limit=500):
        calls['offset'].append(offset)
        return subs if offset == 0 else []

    @asynccontextmanager
    async def fake_lease(subscription_id):
        yield SimpleNamespace(allowed=True, subscription=subs[subscription_id - 1], db=None)

    monkeypatch.setattr('app.database.crud.subscription.get_subscriptions_batch', fake_batch)
    monkeypatch.setattr('app.services.grace_access_runtime.grace_sensitive_panel_update', fake_lease)
    return subs


async def test_bulk_sync_never_resets_devices(_batches, monkeypatch):
    seen: list[dict] = []

    async def fake_push(api, user, subscription, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(action='updated')

    monkeypatch.setattr(runner, 'push_subscription', fake_push)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    stats = await runner.push_all_subscriptions(db, api=object())

    assert stats.updated == 2
    assert all(call['reset_devices'] is False for call in seen)


async def test_throttled_panel_is_a_warning_not_an_error(_batches, monkeypatch):
    fake_logger = MagicMock()
    monkeypatch.setattr(runner, 'logger', fake_logger)

    async def fake_push(api, user, subscription, **kwargs):
        raise RemnaWaveTransientError('Rate limited', 429, {})

    monkeypatch.setattr(runner, 'push_subscription', fake_push)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    stats = await runner.push_all_subscriptions(db, api=object())

    assert stats.errors == 2
    fake_logger.error.assert_not_called()
    assert fake_logger.warning.call_count >= 2


# ---- транзакция чтения не должна жить, пока бот ходит в панель ----


async def test_batch_read_transaction_is_closed_before_panel_writes(_batches, monkeypatch):
    """Живой лог: коммит в конце пачки падал с «connection is closed» — соединение
    отваливалось по простою, пока пачка минутами ходила в панель, и вся пачка
    засчитывалась ошибками, хотя записи в панель уже ушли."""
    order: list[str] = []

    async def fake_batch(db, offset=0, limit=500):
        order.append('fetch')
        return _batches if offset == 0 else []

    async def fake_push(api, user, subscription, **kwargs):
        order.append('push')
        return SimpleNamespace(action='updated')

    async def fake_commit():
        order.append('commit')

    monkeypatch.setattr('app.database.crud.subscription.get_subscriptions_batch', fake_batch)
    monkeypatch.setattr(runner, 'push_subscription', fake_push)
    db = SimpleNamespace(commit=fake_commit, rollback=AsyncMock())

    await runner.push_all_subscriptions(db, api=object())

    assert order.index('commit') < order.index('push')
    for i, event in enumerate(order):
        if event == 'commit':
            assert order[i - 1] == 'fetch', order


async def test_batch_fetch_survives_a_dropped_connection(_batches, monkeypatch):
    from sqlalchemy.exc import InterfaceError

    calls = {'n': 0}

    async def flaky_batch(db, offset=0, limit=500):
        calls['n'] += 1
        if calls['n'] == 1:
            raise InterfaceError('SELECT', {}, Exception('the underlying connection is closed'))
        return _batches if offset == 0 else []

    async def fake_push(api, user, subscription, **kwargs):
        return SimpleNamespace(action='updated')

    monkeypatch.setattr('app.database.crud.subscription.get_subscriptions_batch', flaky_batch)
    monkeypatch.setattr(runner, 'push_subscription', fake_push)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    stats = await runner.push_all_subscriptions(db, api=object())

    assert stats.updated == 2 and stats.errors == 0
    db.rollback.assert_awaited()
