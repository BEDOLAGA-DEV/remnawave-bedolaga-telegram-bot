"""Удаление конкретной подписки пользователя из админской карточки.

В мультитарифном режиме у пользователя несколько подписок, и убрать лишнюю
(например, отработавший триал) можно было только через «Массовые действия»
— в самой карточке кнопки не было.

Тесты фиксируют три границы нового маршрута: чужая подписка не удаляется по
одному лишь ``sub_id`` (IDOR), активная платная защищена от случайного
удаления без ``force``, а открытый временный доступ (grace) отдаёт 409, а не
рвёт подписку под ним.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users


USER_ID = 10
SUB_ID = 777


def _subscription(*, is_active: bool, is_trial: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=SUB_ID,
        user_id=USER_ID,
        tariff_id=1,
        remnawave_id=None,
        is_active=is_active,
        is_trial=is_trial,
        status='active' if is_active else 'expired',
        end_date=datetime.now(UTC) + timedelta(days=5),
    )


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=1, username='admin')


async def test_route_registered(registered_paths):
    """Метод и путь закреплены: иначе маршрут можно переименовать с зелёным CI."""
    assert 'DELETE' in registered_paths.get('/cabinet/admin/users/{user_id}/subscriptions/{sub_id}', set())


def test_force_defaults_to_off():
    """Без явного force активную платную подписку снести нельзя.

    Тесты зовут корутину напрямую и передают force сами, поэтому дефолт
    query-параметра ими не проверяется — а он единственная защита от
    `DELETE .../{sub_id}` без строки запроса.
    """
    import inspect

    default = inspect.signature(admin_users.delete_user_subscription).parameters['force'].default
    assert getattr(default, 'default', default) is False


@pytest.mark.asyncio
async def test_deletes_expired_trial():
    """Базовый случай из отчёта: отработавший триал убирается из карточки."""
    sub = _subscription(is_active=False, is_trial=True)
    deleter = AsyncMock()
    getter = AsyncMock(return_value=sub)
    db = MagicMock()

    with (
        patch('app.database.crud.subscription.get_subscription_by_id_for_user', getter),
        patch('app.services.subscription_deletion_service.delete_subscription_record', deleter),
    ):
        result = await admin_users.delete_user_subscription(USER_ID, SUB_ID, force=False, admin=_admin(), db=db)

    assert result == {'status': 'deleted'}
    assert deleter.await_args.args[1] is sub
    assert deleter.await_args.kwargs['deleted_by'] == 'admin:1'
    # Принадлежность ищется по пользователю ИЗ ПУТИ, а не по админу: id админа
    # (1) и владельца (10) специально разные, чтобы подмена была видна.
    getter.assert_awaited_once_with(db, SUB_ID, USER_ID)


@pytest.mark.asyncio
async def test_foreign_subscription_not_found():
    """Подписка чужого пользователя не удаляется по одному лишь sub_id."""
    with patch('app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await admin_users.delete_user_subscription(USER_ID, SUB_ID, force=False, admin=_admin(), db=MagicMock())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_active_paid_needs_force():
    """Оплаченный активный доступ не сносится одним промахом."""
    sub = _subscription(is_active=True, is_trial=False)
    deleter = AsyncMock()

    with (
        patch('app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=sub)),
        patch('app.services.subscription_deletion_service.delete_subscription_record', deleter),
    ):
        with pytest.raises(HTTPException) as exc:
            await admin_users.delete_user_subscription(USER_ID, SUB_ID, force=False, admin=_admin(), db=MagicMock())
        assert exc.value.status_code == 409
        deleter.assert_not_awaited()

        # С явным force та же подписка удаляется
        result = await admin_users.delete_user_subscription(USER_ID, SUB_ID, force=True, admin=_admin(), db=MagicMock())
    assert result == {'status': 'deleted'}
    deleter.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_grace_blocks_deletion():
    """Пока открыт временный доступ, подписку из-под него не вырывают."""
    from app.services.grace_access_runtime import GraceAccessDeletionBlocked

    sub = _subscription(is_active=False, is_trial=True)

    with (
        patch('app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=sub)),
        patch(
            'app.services.subscription_deletion_service.delete_subscription_record',
            AsyncMock(side_effect=GraceAccessDeletionBlocked([SUB_ID])),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await admin_users.delete_user_subscription(USER_ID, SUB_ID, force=False, admin=_admin(), db=MagicMock())
    assert exc.value.status_code == 409
    assert 'grace' in exc.value.detail.lower()
