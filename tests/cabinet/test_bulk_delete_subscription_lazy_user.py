"""Regression: удаление подписки через «Массовые действия» роняло MissingGreenlet.

Отчёт админа: в мультитарифном режиме при удалении истёкшей подписки
«Пробный» из кабинета приходила ошибка вместо результата:

    File "app/cabinet/routes/admin_bulk_actions.py", line 534, in _do_delete_subscription
      blocked_subscriptions = _build_subscription_info(getattr(user, 'subscriptions', None) or [])
    sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called

Причина: в режиме «по подписке» пользователь приходит из ``sub.user``
(`get_subscription_by_id` грузит только ``user`` и ``tariff``), поэтому его
коллекция подписок не загружена. В async-сессии обращение к незагруженной
коллекции не подтягивает её лениво, а бросает MissingGreenlet — и падало
это ДО того, как подписка вообще удалялась.

Здесь пользователь ведёт себя ровно так же: доступ к ``subscriptions``
кидает MissingGreenlet. Окружение (панель, платёжки, БД) замокано —
проверяем только то, что удаление доходит до конца.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import MissingGreenlet

import app.cabinet.routes.admin_bulk_actions as bulk


class _LazyUser:
    """Пользователь с незагруженной коллекцией подписок — как из sub.user."""

    def __init__(self, user_id: int = 42, username: str = 'NickBerg33') -> None:
        self.id = user_id
        self.username = username
        self.remnawave_id = None

    @property
    def subscriptions(self):
        raise MissingGreenlet('greenlet_spawn has not been called')


def _expired_trial_sub(user) -> SimpleNamespace:
    return SimpleNamespace(
        id=777,
        user=user,
        user_id=user.id,
        tariff_id=1,
        tariff=SimpleNamespace(id=1, name='Пробный'),
        is_active=False,
        is_trial=True,
        remnawave_id=None,
        status='expired',
        end_date=datetime.now(UTC) - timedelta(days=1),
        traffic_used_gb=0,
        traffic_limit_gb=10,
        device_limit=2,
    )


def test_known_subscriptions_falls_back_to_target():
    """Коллекция недоступна → берём целевую подписку, а не падаем."""
    user = _LazyUser()
    sub = _expired_trial_sub(user)
    assert bulk._known_subscriptions(user, sub) == [sub]
    # Без запасной подписки отдаём пустой список, а не исключение
    assert bulk._known_subscriptions(user) == []


def test_known_subscriptions_uses_loaded_collection():
    """Коллекция загружена → отдаём её целиком, запасная не нужна."""
    loaded = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    user = SimpleNamespace(id=1, username='u', subscriptions=loaded)
    assert bulk._known_subscriptions(user, SimpleNamespace(id=99)) == loaded


def test_known_subscriptions_keeps_loaded_empty_list_empty():
    """Загруженный пустой список — это «подписок нет», а не пробел в данных.

    Подстановка сюда целевой подписки соврала бы админу: в отчёте появился бы
    объект, которого у пользователя уже нет.
    """
    user = SimpleNamespace(id=1, username='u', subscriptions=[])
    assert bulk._known_subscriptions(user, SimpleNamespace(id=99)) == []


@pytest.mark.asyncio
async def test_active_paid_skip_reports_target_without_collection():
    """Ветка «активная платная» тоже читает подписки — и тоже не должна падать.

    Это второе место в _do_delete_subscription, где коллекция трогается: сюда
    попадают, когда админ удаляет активную платную подписку без force-флага.
    """
    user = _LazyUser()
    sub = _expired_trial_sub(user)
    sub.is_active = True
    sub.is_trial = False
    params = SimpleNamespace(force_delete_active_paid=False)

    result = await bulk._do_delete_subscription(MagicMock(), user, params, dry_run=False, sub_override=sub)

    assert result.success is False
    assert 'Skipped' in result.message
    assert [info.id for info in result.subscriptions] == [sub.id]


@pytest.mark.asyncio
async def test_execute_for_user_survives_unloaded_collection(monkeypatch):
    """Досборка подписок в _execute_for_user не должна ронять действие.

    Сегодня get_user_by_id грузит коллекцию через selectinload, так что ветка
    молчит. Но она стоит ПОСЛЕ обработчика: если коллекция окажется
    недоступна, уже выполненное действие будет объявлено провалившимся, а
    сессия — откачена. Тест держит эту границу.
    """
    user = _LazyUser()

    async def fake_get_user_by_id(db, uid):
        return user

    async def fake_handler(db, u, params, dry_run):
        return SimpleNamespace(subscriptions=None, success=True, message='ok', user_id=u.id)

    monkeypatch.setattr(bulk, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setitem(bulk._ACTION_HANDLERS, bulk.BulkActionType.CANCEL_SUBSCRIPTION, fake_handler)

    db = MagicMock()
    db.rollback = AsyncMock()

    result = await bulk._execute_for_user(
        db, user.id, bulk.BulkActionType.CANCEL_SUBSCRIPTION, SimpleNamespace(), None, dry_run=False
    )

    assert result.success is True
    assert result.subscriptions == []
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_subscription_survives_unloaded_collection():
    """Удаление истёкшего триала доходит до конца, а не падает на подписках."""
    user = _LazyUser()
    sub = _expired_trial_sub(user)
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    params = SimpleNamespace(force_delete_active_paid=False)

    with (
        patch('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', AsyncMock()),
        patch('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock()),
        patch('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock()),
    ):
        result = await bulk._do_delete_subscription(db, user, params, dry_run=False, sub_override=sub)

    assert result.success is True
    assert 'Пробный' in result.message
    db.commit.assert_awaited()
