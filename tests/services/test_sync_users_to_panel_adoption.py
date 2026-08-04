"""`sync_users_to_panel`: массовая синхронизация бот → панель.

Это единственный путь, который админ может запустить сразу на ВСЮ базу
(«Синхронизировать в панель»). После миграции 0104 и до прогона бэкфила у
каждой строки `remnawave_id IS NULL`, поэтому без подхвата по `shortUuid`
один клик заводил бы дубль панельного аккаунта на каждую подписку и затирал
`remnawave_short_uuid` — единственный ключ, которым оригинал ещё можно найти.

Мутационная проверка показала, что раньше удаление всей этой ветки проходило
при зелёном прогоне, поэтому сценарии пиннятся здесь.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.database.crud.subscription as crud_sub_mod
import app.services.grace_access_runtime as grace_runtime_mod
from app.config import Settings
from app.database.models import SubscriptionStatus
from app.services.remnawave_service import RemnaWaveService


def _user():
    return SimpleNamespace(
        id=1,
        telegram_id=555,
        username='u',
        full_name='User',
        email=None,
        remnawave_id=None,
    )


def _subscription(*, panel_id=None, short_uuid='aBcD12'):
    return SimpleNamespace(
        id=500,
        user=_user(),
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=datetime.now(UTC) + timedelta(days=30),
        traffic_limit_gb=50,
        connected_squads=[],
        tariff=None,
        remnawave_id=panel_id,
        remnawave_short_uuid=short_uuid,
        remnawave_short_id='sid500',
        subscription_url='',
        subscription_crypto_link='',
        device_limit=1,
    )


@pytest.fixture
def harness(monkeypatch):
    """Один батч из одной подписки, gracce-lease разрешён, клиент — мок."""
    subscription = _subscription()
    api = AsyncMock()

    batches = [[subscription], []]

    async def fake_batch(db, offset=0, limit=500):
        return batches.pop(0) if batches else []

    @asynccontextmanager
    async def fake_lease(subscription_id):
        yield SimpleNamespace(allowed=True, subscription=subscription, has_open_grace=False)

    monkeypatch.setattr(crud_sub_mod, 'get_subscriptions_batch', fake_batch)
    monkeypatch.setattr(grace_runtime_mod, 'grace_sensitive_panel_update', fake_lease)

    service = RemnaWaveService()
    service._config_error = None

    @asynccontextmanager
    async def fake_client():
        yield api

    monkeypatch.setattr(service, 'get_api_client', fake_client)
    return SimpleNamespace(service=service, api=api, subscription=subscription)


@pytest.mark.asyncio
async def test_adopts_existing_panel_user_instead_of_creating_a_duplicate(monkeypatch, harness):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    adopted = SimpleNamespace(id=8812, short_uuid='aBcD12', subscription_url='https://s/aBcD12')
    harness.api.get_user_by_short_uuid.return_value = adopted
    harness.api.update_user.return_value = SimpleNamespace(
        id=8812, short_uuid='aBcD12', subscription_url='https://s/aBcD12', happ_crypto_link=None
    )

    stats = await harness.service.sync_users_to_panel(AsyncMock())

    harness.api.create_user.assert_not_awaited()
    harness.api.get_user_by_short_uuid.assert_awaited_once_with('aBcD12')
    assert stats['updated'] == 1 and stats['created'] == 0, 'адопт — это update, а не create'
    # Идентичность обязана сохраниться, иначе СЛЕДУЮЩИЙ синк снова создаст дубль.
    assert harness.subscription.remnawave_id == 8812
    kwargs = harness.api.update_user.await_args.kwargs
    assert kwargs['user_id'] == 8812
    assert 'username' not in kwargs, 'username — create-only; PATCH переименовал бы аккаунт'


@pytest.mark.asyncio
async def test_creates_when_the_panel_does_not_know_the_short_uuid(monkeypatch, harness):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    harness.api.get_user_by_short_uuid.return_value = None
    harness.api.create_user.return_value = SimpleNamespace(
        id=9001, short_uuid='new', subscription_url='https://s/new', happ_crypto_link=None
    )

    stats = await harness.service.sync_users_to_panel(AsyncMock())

    harness.api.create_user.assert_awaited_once()
    assert stats['created'] == 1
    assert harness.subscription.remnawave_id == 9001


@pytest.mark.asyncio
async def test_single_tariff_writes_identity_onto_the_user(monkeypatch, harness):
    """Мутация «поменять ветки местами» схлопывала все подписки юзера на один id.

    В single-tariff сначала работают фолбэки по telegram_id/email — гасим их,
    чтобы дойти именно до подхвата по shortUuid.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    harness.api.find_users_by_telegram_id.return_value = []
    harness.api.find_users_by_email.return_value = []
    adopted = SimpleNamespace(id=8812, short_uuid='aBcD12', subscription_url='https://s/aBcD12')
    harness.api.get_user_by_short_uuid.return_value = adopted
    harness.api.update_user.return_value = SimpleNamespace(
        id=8812, short_uuid='aBcD12', subscription_url='https://s/aBcD12', happ_crypto_link=None
    )

    await harness.service.sync_users_to_panel(AsyncMock())

    assert harness.subscription.user.remnawave_id == 8812
    assert harness.subscription.remnawave_id is None


@pytest.mark.asyncio
async def test_update_branch_does_not_wipe_squads_when_the_local_list_is_empty(monkeypatch, harness):
    """Сиблинг того же дефекта: ветка обновления по УЖЕ известному id.

    `[]` в connected_squads — это дефолт колонки и результат «сброса подписки»,
    а не решение админа. Отправив его в PATCH, синк снял бы у живого аккаунта
    все инбаунды: он остался бы ACTIVE, но конфигов не отдавал бы.
    """
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    harness.subscription.remnawave_id = 8812
    harness.subscription.connected_squads = []
    harness.api.update_user.return_value = SimpleNamespace(
        id=8812, short_uuid='aBcD12', subscription_url='https://s/x', happ_crypto_link=None
    )

    await harness.service.sync_users_to_panel(AsyncMock())

    kwargs = harness.api.update_user.await_args.kwargs
    assert 'active_internal_squads' not in kwargs


@pytest.mark.asyncio
async def test_update_branch_forwards_a_non_empty_squad_list(monkeypatch, harness):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    harness.subscription.remnawave_id = 8812
    harness.subscription.connected_squads = ['squad-1']
    harness.api.update_user.return_value = SimpleNamespace(
        id=8812, short_uuid='aBcD12', subscription_url='https://s/x', happ_crypto_link=None
    )

    await harness.service.sync_users_to_panel(AsyncMock())

    assert harness.api.update_user.await_args.kwargs['active_internal_squads'] == ['squad-1']
