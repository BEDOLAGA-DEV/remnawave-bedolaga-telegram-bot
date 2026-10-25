"""Сводка подписок, у которых в панели больше HWID-устройств, чем разрешает лимит.

Лимит панель применяет только к новым устройствам: после смены тарифа (лимит
сбрасывается до базы нового тарифа), массового действия или правки админом уже
зарегистрированные устройства остаются и работают дальше, а пользователь видит
«N из M». Раз в сутки бот находит такие подписки и сообщает о новых и
изменившихся случаях в категорию «Докупки».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import admin_notification_service as ans, device_limit_overflow as dlo


NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def overflow_settings(monkeypatch):
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', -100123)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ADDONS_ENABLED', True)


class DictCache:
    def __init__(self):
        self.store: dict[str, object] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, expire=None):
        self.store[key] = value
        return True


@pytest.fixture
def fake_cache(monkeypatch) -> DictCache:
    stub = DictCache()
    monkeypatch.setattr(dlo, 'cache', stub)
    return stub


def _sub(sub_id: int, *, limit, user_panel_id=None, sub_panel_id=None, tariff_name='Тариф'):
    user = SimpleNamespace(
        id=sub_id,
        telegram_id=100000 + sub_id,
        email=None,
        username=None,
        first_name=f'User{sub_id}',
        remnawave_id=user_panel_id,
    )
    return SimpleNamespace(
        id=sub_id,
        user=user,
        device_limit=limit,
        remnawave_id=sub_panel_id,
        tariff=SimpleNamespace(name=tariff_name),
    )


def _device(panel_user_id, *, seen_days_ago: int = 1):
    return {'userId': panel_user_id, 'updatedAt': (NOW - timedelta(days=seen_days_ago)).isoformat()}


def test_reports_only_subscriptions_above_their_limit():
    over = _sub(1, limit=1, user_panel_id=11)
    equal = _sub(2, limit=2, user_panel_id=12)
    devices = [_device(11), _device(11), _device(11), _device(12), _device(12)]

    result = dlo.find_device_overflows([over, equal], devices, now=NOW)

    assert [(o.subscription.id, o.devices, o.device_limit) for o in result] == [(1, 3, 1)]


@pytest.mark.parametrize('limit', [0, None])
def test_unlimited_subscription_is_not_checked(limit):
    sub = _sub(1, limit=limit, user_panel_id=11)

    assert dlo.find_device_overflows([sub], [_device(11)] * 5, now=NOW) == []


def test_active_devices_counted_by_last_update():
    sub = _sub(1, limit=1, user_panel_id=11)
    devices = [_device(11, seen_days_ago=2), _device(11, seen_days_ago=45), {'userId': 11}]

    [overflow] = dlo.find_device_overflows([sub], devices, now=NOW)

    assert overflow.devices == 3
    assert overflow.active_devices == 1


def test_multi_tariff_uses_subscription_panel_user(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    # Пользователь панели у пользователя бота свой (другая подписка), у этой — 21.
    sub = _sub(1, limit=1, user_panel_id=11, sub_panel_id=21)
    devices = [_device(11)] * 4 + [_device(21)] * 2

    [overflow] = dlo.find_device_overflows([sub], devices, now=NOW)

    assert overflow.devices == 2


def test_largest_overflow_first():
    small = _sub(1, limit=8, user_panel_id=11)
    large = _sub(2, limit=1, user_panel_id=12)
    devices = [_device(11)] * 9 + [_device(12)] * 4

    result = dlo.find_device_overflows([small, large], devices, now=NOW)

    assert [o.subscription.id for o in result] == [2, 1]


@pytest.mark.asyncio
async def test_case_is_reported_once_until_it_changes(fake_cache):
    sub = _sub(1, limit=1, user_panel_id=11)
    [first] = dlo.find_device_overflows([sub], [_device(11)] * 2, now=NOW)

    assert await dlo.select_unreported([first]) == [first]
    await dlo.mark_reported([first])
    assert await dlo.select_unreported([first]) == []

    [grown] = dlo.find_device_overflows([sub], [_device(11)] * 3, now=NOW)
    assert await dlo.select_unreported([grown]) == [grown]


@pytest.mark.asyncio
async def test_check_sends_unreported_and_remembers_them(monkeypatch, fake_cache):
    subs = [_sub(1, limit=1, user_panel_id=11), _sub(2, limit=1, user_panel_id=12)]
    devices = {'devices': [_device(11)] * 2 + [_device(12)] * 3}

    class FakeApi:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_all_hwid_devices(self):
            return devices

    class FakeService:
        is_configured = True

        def get_api_client(self):
            return FakeApi()

    from app.services import remnawave_service

    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', FakeService)
    monkeypatch.setattr(dlo, 'load_checked_subscriptions', AsyncMock(return_value=subs))
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(ans.AdminNotificationService, 'send_device_limit_overflow_notification', sent)

    assert await dlo.check_device_limit_overflows(db=None, bot=object()) == 2
    assert sent.await_args.kwargs['total'] == 2

    # Ничего не изменилось — повторной сводки нет.
    assert await dlo.check_device_limit_overflows(db=None, bot=object()) == 0
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_check_is_skipped_when_addons_notifications_are_off(monkeypatch, fake_cache):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ADDONS_ENABLED', False)
    loader = AsyncMock()
    monkeypatch.setattr(dlo, 'load_checked_subscriptions', loader)

    assert await dlo.check_device_limit_overflows(db=None, bot=object()) == 0
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_send_is_not_marked_reported(monkeypatch, fake_cache):
    sub = _sub(1, limit=1, user_panel_id=11)

    class FakeApi:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_all_hwid_devices(self):
            return {'devices': [_device(11)] * 2}

    class FakeService:
        is_configured = True

        def get_api_client(self):
            return FakeApi()

    from app.services import remnawave_service

    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', FakeService)
    monkeypatch.setattr(dlo, 'load_checked_subscriptions', AsyncMock(return_value=[sub]))
    monkeypatch.setattr(
        ans.AdminNotificationService, 'send_device_limit_overflow_notification', AsyncMock(return_value=False)
    )

    assert await dlo.check_device_limit_overflows(db=None, bot=object()) == 0
    assert fake_cache.store == {}


@pytest.mark.asyncio
async def test_notification_lists_cases_and_folds_the_rest(monkeypatch):
    service = ans.AdminNotificationService(bot=object())
    captured = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_send_message', captured)
    subs = [_sub(i, limit=1, user_panel_id=100 + i, tariff_name='<Лучшая>') for i in range(1, 31)]
    overflows = dlo.find_device_overflows(subs, [_device(100 + i) for i in range(1, 31)] * 2, now=NOW)

    assert await service.send_device_limit_overflow_notification(overflows, total=31)

    text = captured.await_args.args[0]
    assert captured.await_args.kwargs['category'] == ans.NotificationCategory.ADDONS
    assert 'больше лимита: <b>31</b>, новых или изменившихся: <b>30</b>' in text
    assert '2 устр. при лимите 1' in text
    assert '&lt;Лучшая&gt;' in text
    assert text.count('\n• ') == service.DEVICE_OVERFLOW_MAX_LINES
    assert '… и ещё 5' in text
