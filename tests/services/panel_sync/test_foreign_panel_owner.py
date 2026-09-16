"""Аккаунт панели, записанный за другим человеком, — не наш (GitHub #3245).

У человека было две записи в боте: A — вход по почте, подписка истекла пять
месяцев назад, связи с панелью в базе нет; B — вход через Telegram, живая
оплаченная подписка, её строка держит панельный аккаунт 737. Аккаунт 737 создавался
под A и несёт почту A. Синхронизация A находила 737 по почте, видела там будущую
дату (оплачено B) и гасила её — через пять минут панель слала «истёк», и бот
гасил живую подписку B. Каждое утро.

Корень шире гашения: неточный ключ (почта, Telegram, записанный когда-то id
пользователя, shortUuid) приводил к аккаунту, который база бота уже закрепила за
другим человеком, и бот писал туда всё состояние чужой подписки, а мониторинг —
наоборот, забирал чужой оплаченный срок себе. Теперь такой аккаунт при поиске
пропускается, а если других нет — запись отказывается громко.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import Subscription, SubscriptionStatus, User, UserStatus
from app.services.panel_sync import push_subscription, resolve_panel_identity
from app.services.panel_sync.identity import (
    PanelAccountOwnedByAnotherUser,
    PanelIdentity,
    PanelOwner,
    find_foreign_panel_owner,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = [User.__table__, Subscription.__table__]
NOW = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
PANEL_ID = 737


def _user(user_id: int, *, telegram_id: int | None = None, email: str | None = None, **kw) -> User:
    return User(
        id=user_id,
        telegram_id=telegram_id,
        email=email,
        email_verified=email is not None,
        first_name=f'U{user_id}',
        language='ru',
        status=kw.pop('status', UserStatus.ACTIVE.value),
        balance_kopeks=0,
        **kw,
    )


def _sub(sub_id: int, user_id: int, *, status: str, days: int, **kw) -> Subscription:
    return Subscription(
        id=sub_id,
        user_id=user_id,
        remnawave_short_id=f'sid{sub_id}',
        status=status,
        is_trial=False,
        start_date=NOW - timedelta(days=200),
        end_date=NOW + timedelta(days=days),
        traffic_limit_gb=0,
        device_limit=2,
        connected_squads=[],
        **kw,
    )


async def _seed_issue_3245(db, *, a_user_remnawave_id: int | None = None) -> tuple[User, Subscription]:
    """A — почта, истёкшая подписка без связи; B — Telegram, живая, держит 737."""
    user_a = _user(1, email='user@example.com', remnawave_id=a_user_remnawave_id)
    user_b = _user(2, telegram_id=7012345678)
    sub_a = _sub(11, 1, status=SubscriptionStatus.EXPIRED.value, days=-150)
    sub_b = _sub(12, 2, status=SubscriptionStatus.ACTIVE.value, days=30, remnawave_id=PANEL_ID)
    db.add_all([user_a, user_b, sub_a, sub_b])
    await db.commit()
    return user_a, sub_a


def _panel_737():
    """Аккаунт, оплаченный B: дата в будущем, почта A и Telegram B."""
    return SimpleNamespace(
        id=PANEL_ID,
        username='user_email_user_1',
        short_uuid='s737',
        email='user@example.com',
        telegram_id=7012345678,
        expire_at=NOW + timedelta(days=30),
        subscription_url='https://p/s737',
        happ_crypto_link=None,
    )


def _api(**overrides):
    api = AsyncMock()
    api.get_user_by_id.return_value = None
    api.get_user_by_short_uuid.return_value = None
    api.find_users_by_telegram_id.return_value = []
    api.find_users_by_email.return_value = []
    for key, value in overrides.items():
        getattr(api, key).return_value = value
    return api


# --- кто владеет аккаунтом ---------------------------------------------------


@pytest.mark.asyncio
async def test_account_held_by_another_users_subscription_is_foreign(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db)

        owner = await find_foreign_panel_owner(db, user_a, sub_a, PANEL_ID, multi_tariff=False)

    assert owner is not None
    assert (owner.user_id, owner.subscription_id) == (2, 12)


@pytest.mark.asyncio
async def test_row_ownership_beats_a_user_level_id_picked_up_earlier(monkeypatch) -> None:
    """Прошлая запись по почте успела проставить A ``users.remnawave_id`` — строка B всё равно главнее."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db, a_user_remnawave_id=PANEL_ID)

        owner = await find_foreign_panel_owner(db, user_a, sub_a, PANEL_ID, multi_tariff=False)

    assert owner is not None and owner.user_id == 2


@pytest.mark.asyncio
async def test_the_owner_sees_its_own_account_as_its_own(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_issue_3245(db, a_user_remnawave_id=PANEL_ID)
        user_b = await db.get(User, 2)
        sub_b = await db.get(Subscription, 12)

        owner = await find_foreign_panel_owner(db, user_b, sub_b, PANEL_ID, multi_tariff=False)

    assert owner is None


@pytest.mark.asyncio
async def test_single_tariff_sibling_subscription_of_the_same_person_is_not_foreign(monkeypatch) -> None:
    """В одиночном режиме все подписки человека адресуют один аккаунт — это штатно."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(1, telegram_id=555)
        old = _sub(11, 1, status=SubscriptionStatus.EXPIRED.value, days=-10, remnawave_id=PANEL_ID)
        new = _sub(12, 1, status=SubscriptionStatus.ACTIVE.value, days=30)
        db.add_all([user, old, new])
        await db.commit()

        owner = await find_foreign_panel_owner(db, user, new, PANEL_ID, multi_tariff=False)

    assert owner is None


@pytest.mark.asyncio
async def test_multi_tariff_sibling_subscription_owns_its_own_account(monkeypatch) -> None:
    """В мультитарифе у каждой подписки свой аккаунт: соседняя подписка того же человека — чужой хозяин."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(1, telegram_id=555)
        first = _sub(11, 1, status=SubscriptionStatus.ACTIVE.value, days=30, remnawave_id=PANEL_ID)
        second = _sub(12, 1, status=SubscriptionStatus.EXPIRED.value, days=-10)
        db.add_all([user, first, second])
        await db.commit()

        owner = await find_foreign_panel_owner(db, user, second, PANEL_ID, multi_tariff=True)

    assert owner is not None and owner.subscription_id == 11


@pytest.mark.asyncio
async def test_single_tariff_user_level_link_of_another_person_is_foreign(monkeypatch) -> None:
    """Старые строки одиночного режима: адрес лежит только в ``users.remnawave_id``."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_a = _user(1, email='user@example.com')
        user_b = _user(2, telegram_id=7012345678, remnawave_id=PANEL_ID)
        sub_a = _sub(11, 1, status=SubscriptionStatus.EXPIRED.value, days=-150)
        db.add_all([user_a, user_b, sub_a])
        await db.commit()

        single = await find_foreign_panel_owner(db, user_a, sub_a, PANEL_ID, multi_tariff=False)
        multi = await find_foreign_panel_owner(db, user_a, sub_a, PANEL_ID, multi_tariff=True)

    assert single is not None and (single.user_id, single.subscription_id) == (2, None)
    assert multi is None, 'в мультитарифе id пользователя — мусор из прошлого, не адрес'


@pytest.mark.asyncio
async def test_deleted_person_owns_nothing(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user_a = _user(1, email='user@example.com')
        user_b = _user(2, telegram_id=7012345678, status=UserStatus.DELETED.value)
        sub_a = _sub(11, 1, status=SubscriptionStatus.ACTIVE.value, days=30)
        sub_b = _sub(12, 2, status=SubscriptionStatus.EXPIRED.value, days=-5, remnawave_id=PANEL_ID)
        db.add_all([user_a, user_b, sub_a, sub_b])
        await db.commit()

        owner = await find_foreign_panel_owner(db, user_a, sub_a, PANEL_ID, multi_tariff=False)

    assert owner is None


@pytest.mark.asyncio
async def test_unclaimed_account_is_not_foreign(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db)

        owner = await find_foreign_panel_owner(db, user_a, sub_a, 999, multi_tariff=False)

    assert owner is None


# --- поиск аккаунта -----------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_skips_a_foreign_account_found_by_email(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db)
        api = _api(find_users_by_email=[_panel_737()])

        identity = await resolve_panel_identity(api, user_a, sub_a, multi_tariff=False, db=db)

    assert identity.panel_user is None
    assert identity.user_id is None
    assert identity.foreign_owner is not None and identity.foreign_owner.user_id == 2


@pytest.mark.asyncio
async def test_resolve_skips_a_foreign_user_level_id_and_keeps_searching(monkeypatch) -> None:
    """Записанный когда-то id пользователя ведёт в чужой аккаунт — ищем свой дальше."""
    own = SimpleNamespace(id=812, username='u_own', expire_at=NOW - timedelta(days=150))
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db, a_user_remnawave_id=PANEL_ID)
        api = _api(find_users_by_email=[_panel_737(), own])

        identity = await resolve_panel_identity(api, user_a, sub_a, multi_tariff=False, db=db, verify_recorded_id=False)

    assert identity.user_id == 812
    assert identity.source == 'email'


# --- запись: сценарий из задачи ---------------------------------------------


@pytest.mark.asyncio
async def test_expired_subscription_never_writes_into_someone_elses_paid_account(monkeypatch) -> None:
    """Шаг 3 из задачи: гашение 737 до now+5 мин. Теперь PATCH не уходит вовсе."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db)
        api = _api(find_users_by_email=[_panel_737()])

        with pytest.raises(PanelAccountOwnedByAnotherUser) as caught:
            await push_subscription(api, user_a, sub_a, db=db, multi_tariff=False, verify_recorded_id=False, now=NOW)

        await db.refresh(user_a)
        await db.refresh(sub_a)

    api.update_user.assert_not_awaited()
    api.create_user.assert_not_awaited()
    assert caught.value.panel_user_id == PANEL_ID
    assert caught.value.owner_user_id == 2
    assert user_a.remnawave_id is None, 'чужой адрес не должен прилипнуть к A'
    assert sub_a.remnawave_short_uuid is None


@pytest.mark.asyncio
async def test_known_address_from_the_user_row_is_checked_too(monkeypatch) -> None:
    """Продление передаёт готовый адрес — ``users.remnawave_id``, прилипший от прошлой записи по почте."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db, a_user_remnawave_id=PANEL_ID)
        api = _api()

        with pytest.raises(PanelAccountOwnedByAnotherUser):
            await push_subscription(
                api,
                user_a,
                sub_a,
                db=db,
                multi_tariff=False,
                identity=PanelIdentity(known_id=PANEL_ID),
                create_if_missing=False,
                now=NOW,
            )

    api.update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_owner_still_writes_into_its_own_account(monkeypatch) -> None:
    """Сторож не должен задеть хозяина: B продлевает — PATCH уходит как раньше."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_issue_3245(db)
        user_b = await db.get(User, 2)
        sub_b = await db.get(Subscription, 12)
        api = _api(find_users_by_telegram_id=[_panel_737()])
        api.update_user.return_value = _panel_737()

        result = await push_subscription(api, user_b, sub_b, db=db, multi_tariff=False, now=NOW)

    assert result.action == 'updated'
    assert api.update_user.await_args.kwargs['user_id'] == PANEL_ID


# --- мониторинг: чужой срок себе не забираем ---------------------------------


@pytest.mark.asyncio
async def test_monitoring_does_not_adopt_someone_elses_paid_date(monkeypatch) -> None:
    """Истекает подписка A, по почте находится оплаченный B аккаунт — это не продление A."""
    from contextlib import asynccontextmanager

    from app.services.monitoring_service import MonitoringService

    api = _api(find_users_by_email=[_panel_737()])

    @asynccontextmanager
    async def client():
        yield api

    monitor = MonitoringService.__new__(MonitoringService)
    monitor.subscription_service = SimpleNamespace(is_configured=True, get_api_client=client)
    async with memory_session(monkeypatch, TABLES) as db:
        user_a, sub_a = await _seed_issue_3245(db)
        sub_a.status = SubscriptionStatus.ACTIVE.value
        sub_a.end_date = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
        sub_a.user = user_a

        kept = await MonitoringService._panel_keeps_alive(monitor, db, sub_a)
        await db.refresh(sub_a)

    assert kept is False
    assert sub_a.end_date.replace(tzinfo=UTC) < datetime.now(UTC), 'срок B не должен переехать в A'


# --- массовый проход «в панель» ----------------------------------------------


def _runner_batches(monkeypatch, subs):
    from contextlib import asynccontextmanager

    async def fake_batch(db, offset=0, limit=500):
        return subs if offset == 0 else []

    @asynccontextmanager
    async def fake_lease(subscription_id):
        yield SimpleNamespace(allowed=True, subscription=next(s for s in subs if s.id == subscription_id), db=None)

    monkeypatch.setattr('app.database.crud.subscription.get_subscriptions_batch', fake_batch)
    monkeypatch.setattr('app.services.grace_access_runtime.grace_sensitive_panel_update', fake_lease)


@pytest.mark.asyncio
async def test_bulk_push_skips_a_foreign_account_without_an_error(monkeypatch) -> None:
    from app.services.panel_sync import runner

    subs = [SimpleNamespace(id=11, user=SimpleNamespace(id=1, status='active', telegram_id=None))]
    _runner_batches(monkeypatch, subs)

    async def fake_push(api, user, subscription, **kwargs):
        raise PanelAccountOwnedByAnotherUser(
            subscription_id=subscription.id, panel_user_id=PANEL_ID, owner=PanelOwner(user_id=2, subscription_id=12)
        )

    monkeypatch.setattr(runner, 'push_subscription', fake_push)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    stats = await runner.push_all_subscriptions(db, api=object())

    assert (stats.skipped, stats.errors) == (1, 0)


@pytest.mark.asyncio
async def test_bulk_push_leaves_deleted_people_alone(monkeypatch) -> None:
    """Мягко удалённая запись A продолжала гасить чужой аккаунт — обходной путь из задачи не работал."""
    from app.services.panel_sync import runner

    subs = [
        SimpleNamespace(id=11, user=SimpleNamespace(id=1, status=UserStatus.DELETED.value, telegram_id=None)),
        SimpleNamespace(id=12, user=SimpleNamespace(id=2, status=UserStatus.ACTIVE.value, telegram_id=7)),
    ]
    _runner_batches(monkeypatch, subs)
    pushed: list[int] = []

    async def fake_push(api, user, subscription, **kwargs):
        pushed.append(subscription.id)
        return SimpleNamespace(action='updated')

    monkeypatch.setattr(runner, 'push_subscription', fake_push)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    stats = await runner.push_all_subscriptions(db, api=object())

    assert pushed == [12]
    assert stats.updated == 1
