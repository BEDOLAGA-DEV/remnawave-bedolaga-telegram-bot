"""Штатное обновление панели проставляет свежей строке id аккаунта, который обновило.

В single-tariff панель адресуется через ``users.remnawave_id``, и новая строка
подписки (создана после удаления старой или повторной покупкой) оставалась с
пустым ``subscriptions.remnawave_id``. Админские экраны по выбранной подписке
читают строго его — «пользователь не найден в панели», без устройств и трафика.
Колонка частично уникальна: если id держит соседняя строка, не пишем.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Только модулем: тест подменяет его атрибуты (get_user_by_id и другие), и второй
# импорт тех же имён через `from` разошёлся бы с подменой — CodeQL ровно об этом
# и предупреждает (py/import-and-import-from).
import app.services.subscription_service as subscription_service_mod
from app.config import Settings
from app.database.models import SubscriptionStatus


USER_PANEL_ID = 777


def _user(**overrides) -> SimpleNamespace:
    base = {
        'id': 1,
        'telegram_id': 100,
        'username': 'u',
        'full_name': 'User',
        'email': None,
        'remnawave_id': USER_PANEL_ID,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 11,
        'user_id': 1,
        'status': SubscriptionStatus.ACTIVE.value,
        'end_date': datetime.now(UTC) + timedelta(days=10),
        'traffic_limit_gb': 100,
        'connected_squads': [],
        'tariff': None,
        'remnawave_id': None,
        'remnawave_short_uuid': None,
        'is_trial': False,
        'last_webhook_update_at': None,
        'subscription_url': None,
        'subscription_crypto_link': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _db(*, holder) -> AsyncMock:
    """Единственный SELECT привязки — «держит ли id другая строка»."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = holder
    result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _service(monkeypatch, api, user) -> subscription_service_mod.SubscriptionService:
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    service = subscription_service_mod.SubscriptionService()
    monkeypatch.setattr(subscription_service_mod, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(subscription_service_mod, 'resolve_hwid_device_limit_for_payload', lambda s: None)

    @asynccontextmanager
    async def fake_client():
        yield api

    monkeypatch.setattr(service, 'get_api_client', fake_client)
    return service


def _api() -> AsyncMock:
    api = AsyncMock()
    api.update_user.return_value = SimpleNamespace(subscription_url='https://sub.example/u', happ_crypto_link=None)
    return api


async def test_update_links_fresh_row_to_the_account_it_updated(monkeypatch):
    api = _api()
    service = _service(monkeypatch, api, _user())
    subscription = _subscription()
    db = _db(holder=None)

    result = await service.update_remnawave_user(db, subscription)

    assert result is not None
    assert api.update_user.await_args.kwargs['user_id'] == USER_PANEL_ID
    assert subscription.remnawave_id == USER_PANEL_ID
    db.commit.assert_awaited()


async def test_update_leaves_row_unlinked_when_sibling_row_holds_the_account(monkeypatch):
    service = _service(monkeypatch, _api(), _user())
    subscription = _subscription()

    result = await service.update_remnawave_user(_db(holder=12), subscription)

    assert result is not None
    assert subscription.remnawave_id is None


async def test_link_is_noop_for_already_linked_row():
    db = _db(holder=None)
    subscription = _subscription(remnawave_id=555)

    assert await subscription_service_mod.link_subscription_panel_identity(db, subscription, USER_PANEL_ID) is False
    db.execute.assert_not_awaited()
    assert subscription.remnawave_id == 555
