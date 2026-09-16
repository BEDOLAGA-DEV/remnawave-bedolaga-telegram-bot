"""Мониторинг не переносит оверлей грейса, открытого, пока он шёл по списку.

Стенд 2026-09-15 (панель 3.4.3, волна истечения на 96 человек): при одновременной
работе воркера грейса и мониторинга у 7 человек дата грейса, его сквад и лимит
«расход + 1 ГБ» оказались в подписке бота — ровно симптом жалобы, хотя признак
``grace_session_open`` уже защищал импорт.

Корень — порядок чтения. Мониторинг берёт список истёкших подписок в начале прохода
и дальше идёт по нему с запросом в панель на каждую. Воркер успевает выдать грейс
подписке из хвоста списка: признак в базе уже ``true``, а в объекте из списка всё
ещё ``false``. Панель к этому моменту показывает оверлей — и проекция его забирает.

Хранилище сессий фиксирует признак ДО того, как оверлей уходит в панель, поэтому
признак, прочитанный из базы ПОСЛЕ снимка панели, всегда видит грейс, который этот
снимок мог показать.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, User, UserStatus
from app.services import monitoring_service as monitoring_module
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from tests.fixtures.sqlite_memory import memory_session


TABLES = [User.__table__, Subscription.__table__]
GIB = 1024**3
PANEL_ID = 9001
TARIFF_SQUAD = '11111111-1111-1111-1111-111111111111'
GRACE_SQUAD = '22222222-2222-2222-2222-222222222222'


def _overlay_panel_user(expire_at: datetime):
    """Так панель выглядит во время грейса: ACTIVE до конца грейса, сквад грейса, «расход + 1 ГБ»."""
    return SimpleNamespace(
        id=PANEL_ID,
        status='ACTIVE',
        expire_at=expire_at,
        used_traffic_bytes=7 * GIB,
        traffic_limit_bytes=8 * GIB,
        hwid_device_limit=3,
        active_internal_squads=[GRACE_SQUAD],
        short_uuid='abc',
        subscription_url='https://sub',
        happ_crypto_link=None,
    )


def _monitor(panel_user) -> MonitoringService:
    api = SimpleNamespace(
        get_user_by_id=AsyncMock(return_value=panel_user),
        get_user_by_short_uuid=AsyncMock(return_value=None),
        find_users_by_telegram_id=AsyncMock(return_value=[]),
        find_users_by_email=AsyncMock(return_value=[]),
    )

    @asynccontextmanager
    async def client():
        yield api

    service = MonitoringService.__new__(MonitoringService)
    service.bot = None
    service.subscription_service = SimpleNamespace(is_configured=True, get_api_client=client)
    service._log_monitoring_event = AsyncMock()
    return service


@pytest.fixture(autouse=True)
def _wiring(monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(
        NotificationSettingsService, 'are_notifications_globally_enabled', classmethod(lambda cls: True)
    )
    monkeypatch.setattr('app.database.crud.subscription.is_recently_updated_by_webhook', lambda subscription: False)


async def _seed(db) -> tuple[User, Subscription, datetime]:
    end_date = datetime.now(UTC) - timedelta(minutes=1)
    user = User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status=UserStatus.ACTIVE.value,
        balance_kopeks=0,
        remnawave_id=PANEL_ID,
    )
    subscription = Subscription(
        id=7,
        user_id=1,
        remnawave_short_id='sid7',
        status=SubscriptionStatus.ACTIVE.value,  # мониторинг ещё не гасил
        is_trial=False,
        start_date=end_date - timedelta(days=30),
        end_date=end_date,
        traffic_limit_gb=0,
        traffic_used_gb=7.0,
        device_limit=3,
        connected_squads=[TARIFF_SQUAD],
        remnawave_id=PANEL_ID,
    )
    db.add_all([user, subscription])
    await db.commit()
    subscription.user = user
    return user, subscription, end_date


@pytest.mark.asyncio
async def test_grace_opened_while_monitoring_walked_its_list_is_not_imported(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription, end_date = await _seed(db)
        # Мониторинг взял список, пока грейса ещё не было…
        monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
        monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
        # …а воркер выдал его, пока мониторинг шёл по списку: признак в базе уже
        # true, объект из списка его не видит, панель показывает оверлей.
        # Запись воркера идёт из другой сессии — объект этой сессии её не видит.
        await db.execute(
            update(Subscription)
            .where(Subscription.id == 7)
            .values(grace_session_open=True)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        assert subscription.grace_session_open is False, 'предусловие: объект из списка устарел'
        monitor = _monitor(_overlay_panel_user(datetime.now(UTC) + timedelta(hours=72)))

        await monitor._check_expired_subscriptions(db)
        await db.refresh(subscription)

    assert abs((subscription.end_date.replace(tzinfo=UTC) - end_date).total_seconds()) < 1, (
        'дата грейса стала датой подписки'
    )
    assert subscription.connected_squads == [TARIFF_SQUAD], 'сквад грейса стал сквадом подписки'
    assert subscription.traffic_limit_gb == 0, 'лимит грейса стал лимитом подписки'
    assert subscription.status == SubscriptionStatus.EXPIRED.value, 'подписку гасят по своей дате'


@pytest.mark.asyncio
async def test_renewal_that_landed_while_monitoring_walked_its_list_is_not_expired(monkeypatch) -> None:
    """Тот же устаревший список: подписку уже продлили — гасить её нельзя.

    Объект из списка помнит прошедшую дату и ACTIVE; продление успело записать новую
    дату. Раньше гашение ставило EXPIRED оплаченной подписке (панель в ответ показывала
    живой аккаунт, но решение принималось по устаревшему объекту).
    """
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription, _ = await _seed(db)
        monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
        monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
        renewed_until = datetime.now(UTC) + timedelta(days=30)
        await db.execute(
            update(Subscription)
            .where(Subscription.id == 7)
            .values(end_date=renewed_until)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        expired_panel_user = _overlay_panel_user(datetime.now(UTC) - timedelta(minutes=1))
        expired_panel_user.status = 'EXPIRED'
        monitor = _monitor(expired_panel_user)

        await monitor._check_expired_subscriptions(db)
        await db.refresh(subscription)

    assert subscription.status == SubscriptionStatus.ACTIVE.value, 'продлённую подписку погасили'
    assert subscription.end_date.replace(tzinfo=UTC) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_expiry_does_not_overwrite_a_renewal_committed_right_before_it(monkeypatch) -> None:
    """Последнее окно: продление записано уже после перечитывания, но до гашения.

    Гашение ставит EXPIRED одним условным UPDATE — только если в базе подписка всё
    ещё ACTIVE с прошедшей датой; устаревший объект в памяти ничего не решает.
    """
    from app.database.crud.subscription import expire_subscription_if_still_due

    async with memory_session(monkeypatch, TABLES) as db:
        _, subscription, _ = await _seed(db)
        await db.execute(
            update(Subscription)
            .where(Subscription.id == 7)
            .values(end_date=datetime.now(UTC) + timedelta(days=30))
            .execution_options(synchronize_session=False)
        )
        await db.commit()

        expired = await expire_subscription_if_still_due(db, subscription)

    assert expired is False
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.end_date.replace(tzinfo=UTC) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_expiry_still_expires_a_subscription_that_is_due(monkeypatch) -> None:
    from app.database.crud.subscription import expire_subscription_if_still_due

    async with memory_session(monkeypatch, TABLES) as db:
        _, subscription, _ = await _seed(db)

        expired = await expire_subscription_if_still_due(db, subscription)

    assert expired is True
    assert subscription.status == SubscriptionStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_grace_closed_early_between_snapshot_and_reread_is_not_imported(monkeypatch) -> None:
    """Ревью 2026-09-15: снимок панели — ещё оверлей, а к перечитыванию грейс уже закрыт досрочно.

    Досрочное закрытие (конфликт, отзыв, слив) снимает признак, а хвостом ставит
    «ближайший допустимый момент» — не дату оверлея. Дата оверлея на подписке
    остаётся и узнаёт запоздалый снимок.
    """
    overlay_until = datetime.now(UTC) + timedelta(hours=71)
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription, end_date = await _seed(db)
        monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
        monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
        await db.execute(
            update(Subscription)
            .where(Subscription.id == 7)
            .values(
                grace_session_open=False,
                grace_overlay_expire_at=overlay_until,
                grace_tail_expire_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        monitor = _monitor(_overlay_panel_user(overlay_until))

        await monitor._check_expired_subscriptions(db)
        await db.refresh(subscription)

    assert abs((subscription.end_date.replace(tzinfo=UTC) - end_date).total_seconds()) < 1
    assert subscription.connected_squads == [TARIFF_SQUAD]
    assert subscription.traffic_limit_gb == 0
    assert subscription.status == SubscriptionStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_one_broken_subscription_does_not_stop_the_whole_pass(monkeypatch) -> None:
    """Ревью: сбой на одной подписке (удалили во время прохода) прерывал гашение всех остальных."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription, _ = await _seed(db)
        ghost = Subscription(id=999, user_id=1, status='active', end_date=datetime.now(UTC) - timedelta(days=1))
        monkeypatch.setattr(
            monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[ghost, subscription])
        )
        monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
        expired_panel_user = _overlay_panel_user(datetime.now(UTC) - timedelta(minutes=1))
        expired_panel_user.status = 'EXPIRED'

        await _monitor(expired_panel_user)._check_expired_subscriptions(db)
        await db.refresh(subscription)

    assert subscription.status == SubscriptionStatus.EXPIRED.value
