"""«Удалил подписку → купил заново» в мультитарифе не наследует удалённый аккаунт панели.

Репорт: человек удаляет подписку в кабинете, бот удаляет его аккаунт в панели.
Через минуту он покупает тариф — и новая строка получает ``remnawave_id``
удалённого аккаунта: тот остался в ``users.remnawave_id``, а правило
``should_create_panel_account`` привязывает «свободный аккаунт человека» к
строке без id. Дальше PATCH в панель отвечает «User not found», и всё, что
адресует подписку по этому id, сыплет ошибками в админ-чат.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, User
from app.external.remnawave_api import RemnaWaveAPIError
from app.services import subscription_deletion_service as deletion
from app.services.panel_sync import should_create_panel_account
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
OLD_PANEL_ID = 555
NEW_PANEL_ID = 777


@pytest.fixture
def multi_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'REMNAWAVE_USER_DELETE_MODE', 'delete')


def _subscription(user_id: int, *, panel_id: int | None, short_id: str) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        tariff_id=None,
        start_date=now,
        end_date=now + timedelta(days=30),
        traffic_limit_gb=10,
        device_limit=2,
        connected_squads=[],
        remnawave_id=panel_id,
        remnawave_short_id=short_id,
    )


def _panel_user(panel_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=panel_id,
        short_uuid=f'short-{panel_id}',
        subscription_url=f'https://panel/{panel_id}',
        happ_crypto_link='',
        expire_at=datetime.now(UTC) + timedelta(days=30),
    )


def _panel_without_the_old_account() -> AsyncMock:
    """Панель после удаления: старого аккаунта нет, новый создаётся."""
    api = AsyncMock()
    api.get_user_by_id.return_value = None
    api.get_user_by_short_uuid.return_value = None
    api.find_users_by_telegram_id.return_value = []
    api.find_users_by_email.return_value = []
    api.update_user.side_effect = RemnaWaveAPIError('User not found', 404, {'errorCode': 'A063'})
    api.create_user.return_value = _panel_user(NEW_PANEL_ID)
    return api


def _quiet_side_effects(monkeypatch, api: AsyncMock) -> None:
    """Грейс, автоплатежи и счётчики серверов живут в своих таблицах и к сценарию не относятся."""
    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        'app.services.grace_access_runtime.lock_grace_sensitive_panel_updates', AsyncMock(return_value=set())
    )
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr(deletion, 'decrement_subscription_server_counts', AsyncMock())
    monkeypatch.setattr(
        'app.services.remnawave_webhook_service.RemnaWaveWebhookService.mark_intentional_panel_deletion',
        lambda **_: None,
    )
    monkeypatch.setattr(SubscriptionService, 'validate_and_clean_subscription', AsyncMock(return_value=True))

    @contextlib.asynccontextmanager
    async def _client(self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _client)


async def _seed_user_with_subscription(db) -> tuple[User, Subscription]:
    user = User(email='google@example.com', first_name='Google', language='ru', status='active')
    user.remnawave_id = OLD_PANEL_ID
    db.add(user)
    await db.flush()
    sub = _subscription(user.id, panel_id=OLD_PANEL_ID, short_id='old')
    db.add(sub)
    await db.commit()
    return user, sub


@pytest.mark.asyncio
async def test_deleting_the_subscription_forgets_the_deleted_panel_account(monkeypatch, multi_tariff):
    api = _panel_without_the_old_account()
    _quiet_side_effects(monkeypatch, api)
    async with memory_session(monkeypatch, TABLES) as db:
        user, sub = await _seed_user_with_subscription(db)

        await deletion.delete_subscription_record(db, sub, deleted_by='owner')

        await db.refresh(user)
        assert user.remnawave_id is None


@pytest.mark.asyncio
async def test_repurchase_gets_a_fresh_panel_account(monkeypatch, multi_tariff):
    api = _panel_without_the_old_account()
    _quiet_side_effects(monkeypatch, api)
    async with memory_session(monkeypatch, TABLES) as db:
        user, old_sub = await _seed_user_with_subscription(db)
        await deletion.delete_subscription_record(db, old_sub, deleted_by='owner')

        new_sub = _subscription(user.id, panel_id=None, short_id='new')
        db.add(new_sub)
        await db.commit()

        assert await should_create_panel_account(db, new_sub, user) is True
        assert new_sub.remnawave_id is None


@pytest.mark.asyncio
async def test_stale_user_level_id_is_healed_by_the_update_path(monkeypatch, multi_tariff):
    """База уже в битом состоянии (подписку удалили до фикса): покупка обязана сама выйти на новый аккаунт."""
    api = _panel_without_the_old_account()
    _quiet_side_effects(monkeypatch, api)
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(email='google@example.com', first_name='Google', language='ru', status='active')
        user.remnawave_id = OLD_PANEL_ID
        db.add(user)
        await db.flush()
        new_sub = _subscription(user.id, panel_id=None, short_id='new')
        db.add(new_sub)
        await db.commit()

        service = SubscriptionService()
        if await should_create_panel_account(db, new_sub, user):
            await service.create_remnawave_user(db, new_sub)
        else:
            await service.update_remnawave_user(db, new_sub)

        await db.refresh(new_sub)
        await db.refresh(user)
        assert new_sub.remnawave_id == NEW_PANEL_ID
        assert user.remnawave_id == NEW_PANEL_ID
