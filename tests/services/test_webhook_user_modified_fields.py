"""Правила события ``user.modified`` от панели.

Ручка вебхуков была покрыта только на уровне HTTP (подпись, разбор события), а
её правила синхронизации полей — нет. Здесь они закреплены: событие свежее
любого снимка, поэтому дата и лимит трафика берутся при любом статусе панели, но

* подписку, намеренно отключённую в боте (обнуление админом), вебхук не
  воскрешает: у панели могла остаться старая дата, и списанные дни вернулись бы;
* истёкшей подписку вебхук не объявляет — это работа мониторинга с его буфером
  и уведомлениями;
* ссылки приходят по сети, поэтому непрошедшие проверку не сохраняются: иначе в
  базу попадает чужой адрес и уезжает пользователю;
* пока открыт грейс, дата, статус и лимит — собственность бота.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import SubscriptionStatus
from app.services import remnawave_webhook_service as webhook_mod
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


NOW = datetime.now(UTC)


@pytest.fixture
def service(monkeypatch):
    async def no_grace(_db):
        return set()

    monkeypatch.setattr(webhook_mod, 'get_open_grace_subscription_ids', no_grace)
    return RemnaWaveWebhookService(bot=SimpleNamespace())


def _subscription(**kw):
    base = dict(
        id=10,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        traffic_limit_gb=100,
        traffic_used_gb=1.0,
        device_limit=3,
        connected_squads=['sq1'],
        remnawave_short_uuid='short',
        subscription_url='https://old/sub',
        subscription_crypto_link='old-crypto',
        grace_candidate_reason=None,
        grace_candidate_at=None,
        last_webhook_update_at=None,
        updated_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _db():
    db = AsyncMock()
    db.commit = AsyncMock()
    return db


def _payload(**kw):
    base = {
        'status': 'ACTIVE',
        'expireAt': (NOW + timedelta(days=90)).isoformat().replace('+00:00', 'Z'),
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_panel_date_wins_for_a_live_subscription(service):
    subscription = _subscription()

    await service._handle_user_modified(_db(), SimpleNamespace(id=1), subscription, _payload())

    assert abs((subscription.end_date - (NOW + timedelta(days=90))).total_seconds()) < 2
    assert subscription.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_disabled_subscription_does_not_get_its_days_back(service):
    """Обнуление админом: старая дата из панели вернула бы списанные дни."""
    subscription = _subscription(status=SubscriptionStatus.DISABLED.value, end_date=NOW)

    await service._handle_user_modified(_db(), SimpleNamespace(id=1), subscription, _payload())

    assert subscription.end_date == NOW


@pytest.mark.asyncio
async def test_panel_disabled_disables_the_subscription(service):
    subscription = _subscription()

    await service._handle_user_modified(_db(), SimpleNamespace(id=1), subscription, _payload(status='DISABLED'))

    assert subscription.status == SubscriptionStatus.DISABLED.value


@pytest.mark.asyncio
async def test_webhook_never_declares_a_subscription_expired(service):
    """Истечение объявляет мониторинг: у него буфер и уведомления."""
    subscription = _subscription(end_date=NOW - timedelta(days=1))

    await service._handle_user_modified(
        _db(),
        SimpleNamespace(id=1),
        subscription,
        _payload(status='EXPIRED', expireAt=(NOW - timedelta(days=1)).isoformat().replace('+00:00', 'Z')),
    )

    assert subscription.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_traffic_limit_and_nested_usage_are_synced(service):
    """Расширенная схема панели прячет расход в userTraffic, плоского поля там нет."""
    subscription = _subscription()

    await service._handle_user_modified(
        _db(),
        SimpleNamespace(id=1),
        subscription,
        _payload(trafficLimitBytes=250 * 1024**3, userTraffic={'usedTrafficBytes': 5 * 1024**3}),
    )

    assert subscription.traffic_limit_gb == 250
    assert subscription.traffic_used_gb == 5.0


@pytest.mark.asyncio
async def test_device_limit_is_never_taken_from_a_webhook(service):
    """Лимит устройств задаёт тариф в боте."""
    subscription = _subscription()

    await service._handle_user_modified(_db(), SimpleNamespace(id=1), subscription, _payload(hwidDeviceLimit=99))

    assert subscription.device_limit == 3


@pytest.mark.asyncio
async def test_a_link_that_fails_validation_is_not_stored(service):
    subscription = _subscription()

    await service._handle_user_modified(
        _db(),
        SimpleNamespace(id=1),
        subscription,
        _payload(subscriptionUrl='javascript:alert(1)'),
    )

    assert subscription.subscription_url == 'https://old/sub'


@pytest.mark.asyncio
async def test_a_valid_link_replaces_the_stored_one(service):
    subscription = _subscription()

    await service._handle_user_modified(
        _db(),
        SimpleNamespace(id=1),
        subscription,
        _payload(subscriptionUrl='https://panel.example/sub/new'),
    )

    assert subscription.subscription_url == 'https://panel.example/sub/new'


@pytest.mark.asyncio
async def test_every_event_stamps_the_subscription(service):
    """Метка защищает свежие данные от затирания медленным полным проходом."""
    subscription = _subscription()

    await service._handle_user_modified(_db(), SimpleNamespace(id=1), subscription, _payload())

    assert subscription.last_webhook_update_at is not None


@pytest.mark.asyncio
async def test_open_grace_freezes_the_billing_fields(monkeypatch):
    async def grace_is_open(_db):
        return {10}

    monkeypatch.setattr(webhook_mod, 'get_open_grace_subscription_ids', grace_is_open)
    service = RemnaWaveWebhookService(bot=SimpleNamespace())
    subscription = _subscription()
    original_end = subscription.end_date

    await service._handle_user_modified(
        _db(),
        SimpleNamespace(id=1),
        subscription,
        _payload(status='DISABLED', trafficLimitBytes=1024**3, userTraffic={'usedTrafficBytes': 7 * 1024**3}),
    )

    assert subscription.end_date == original_end
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.traffic_limit_gb == 100
    assert subscription.traffic_used_gb == 7.0, 'расход показываем: он ничего не решает'
