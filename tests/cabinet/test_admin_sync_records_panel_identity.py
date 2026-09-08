"""Админская синхронизация записывает id панели на строку подписки.

Отчёт: смена тарифа через админку (удалить подписку → создать новую) в одиночном
режиме тарифов оставляла ``subscriptions.remnawave_id`` пустым, хотя
``users.remnawave_id`` на месте. Помощник находил аккаунт панели по пользователю,
обновлял его, но id на новую строку не писал — а экран подписки в админке читает
строго id выбранной подписки: «пользователь не найден в панели», без устройств и
трафика. Лечилось руками в базе. То же — у ``sync/to-panel`` со своей копией логики.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import SyncToPanelRequest
from app.config import Settings


USER_PANEL_ID = 7001
NEW_PANEL_ID = 7002


def _user(**overrides) -> SimpleNamespace:
    base = {
        'id': 10,
        'full_name': 'Owner',
        'username': None,
        'telegram_id': 1000,
        'email': None,
        'remnawave_id': USER_PANEL_ID,
        'last_remnawave_sync': None,
        'updated_at': None,
        'subscriptions': [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 101,
        'user_id': 10,
        'status': 'active',
        'is_active': True,
        'end_date': datetime.now(UTC) + timedelta(days=30),
        'remnawave_id': None,
        'remnawave_short_id': None,
        'remnawave_short_uuid': None,
        'traffic_limit_gb': 10,
        'tariff': None,
        'connected_squads': [],
        'device_limit': 1,
        'subscription_url': None,
        'subscription_crypto_link': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Api:
    def __init__(self, known_ids) -> None:
        self.known_ids = set(known_ids)

    async def get_user_by_id(self, panel_user_id):
        return SimpleNamespace(id=panel_user_id) if panel_user_id in self.known_ids else None

    async def find_users_by_telegram_id(self, _telegram_id):
        return []

    async def find_users_by_email(self, _email):
        return []


def _panel_double(monkeypatch, api, *, multi_tariff: bool) -> tuple[list, list]:
    updated: list = []
    created: list = []

    async def update_panel_user(_api, _sub_id, **kwargs):
        updated.append(kwargs['user_id'])
        return SimpleNamespace(subscription_url='https://p.example/sub', happ_crypto_link='crypto', short_uuid='s1')

    async def create_panel_user(_api, _sub_id, **kwargs):
        created.append(kwargs.get('username'))
        return SimpleNamespace(
            id=NEW_PANEL_ID, subscription_url='https://p.example/new', happ_crypto_link='c2', short_uuid='s2'
        )

    class Service:
        is_configured = True

        def get_api_client(self):
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=api)
            context.__aexit__ = AsyncMock(return_value=None)
            return context

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: multi_tariff)
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', Service)
    monkeypatch.setattr('app.services.grace_access_runtime.update_panel_user_grace_safe', update_panel_user)
    monkeypatch.setattr('app.services.grace_access_runtime.create_panel_user_grace_safe', create_panel_user)
    monkeypatch.setattr('app.services.subscription_service.get_traffic_reset_strategy', lambda _tariff: 'NO_RESET')
    monkeypatch.setattr('app.utils.subscription_utils.resolve_hwid_device_limit_for_payload', lambda _sub: None)
    return updated, created


def _db(*, panel_id_taken: bool) -> AsyncMock:
    """SELECT «держит ли id другая строка» — единственный запрос помощника к базе."""
    db = AsyncMock()
    holder = 999 if panel_id_taken else None
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: holder))
    return db


async def test_single_mode_update_records_user_panel_id_on_new_subscription(monkeypatch):
    updated, created = _panel_double(monkeypatch, _Api({USER_PANEL_ID}), multi_tariff=False)
    user, subscription = _user(), _subscription()
    db = _db(panel_id_taken=False)

    changes = await admin_users._sync_subscription_to_panel(db, user, subscription)

    assert changes['action'] == 'updated'
    assert updated == [USER_PANEL_ID]
    assert created == []
    assert subscription.remnawave_id == USER_PANEL_ID
    assert changes['panel_user_id'] == USER_PANEL_ID
    db.commit.assert_awaited()


async def test_single_mode_leaves_row_unlinked_when_another_row_holds_the_id(monkeypatch):
    """Колонка частично уникальна: id у соседней строки — не пишем и не падаем."""
    _panel_double(monkeypatch, _Api({USER_PANEL_ID}), multi_tariff=False)
    user, subscription = _user(), _subscription()

    changes = await admin_users._sync_subscription_to_panel(_db(panel_id_taken=True), user, subscription)

    assert changes['action'] == 'updated'
    assert subscription.remnawave_id is None
    assert user.remnawave_id == USER_PANEL_ID


async def test_multi_mode_new_subscription_gets_its_own_panel_user(monkeypatch):
    updated, created = _panel_double(monkeypatch, _Api({USER_PANEL_ID}), multi_tariff=True)
    user, subscription = _user(), _subscription()

    changes = await admin_users._sync_subscription_to_panel(_db(panel_id_taken=False), user, subscription)

    assert changes['action'] == 'created'
    assert created
    assert updated == []
    assert subscription.remnawave_id == NEW_PANEL_ID
    assert user.remnawave_id == USER_PANEL_ID


async def test_sync_to_panel_endpoint_records_panel_id_on_selected_subscription(monkeypatch):
    updated, _created = _panel_double(monkeypatch, _Api({USER_PANEL_ID}), multi_tariff=False)
    subscription = _subscription()
    user = _user(subscriptions=[subscription])
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))

    result = await admin_users.sync_user_to_panel(
        user.id,
        subscription_id=subscription.id,
        request=SyncToPanelRequest(),
        admin=SimpleNamespace(id=1),
        db=_db(panel_id_taken=False),
    )

    assert result.action == 'updated'
    assert updated == [USER_PANEL_ID]
    assert subscription.remnawave_id == USER_PANEL_ID
