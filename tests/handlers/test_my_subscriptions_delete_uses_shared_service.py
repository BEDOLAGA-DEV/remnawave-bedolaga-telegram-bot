"""Самоудаление подписки из бота идёт через общий сервис удаления.

Обработчик держал собственную копию порядка «грейс → автоплатежи → панель →
строка» и звал delete_user напрямую — мимо REMNAWAVE_USER_DELETE_MODE и мимо
правил адресации аккаунта. Теперь единственный владелец порядка —
``delete_subscription_record``; обработчику остаётся ответ пользователю.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import SubscriptionStatus
from app.handlers.subscription import my_subscriptions


def _subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=99,
        user_id=1,
        tariff_id=None,
        status=SubscriptionStatus.EXPIRED.value,
        actual_status=SubscriptionStatus.EXPIRED.value,
        remnawave_id=4242,
    )


@pytest.fixture
def handler_env(monkeypatch):
    subscription = _subscription()
    monkeypatch.setattr(my_subscriptions, 'get_subscription_by_id_for_user', AsyncMock(return_value=subscription))
    monkeypatch.setattr(my_subscriptions, 'show_my_subscriptions', AsyncMock())
    callback = SimpleNamespace(data='sub_del_yes:99', answer=AsyncMock())
    return subscription, callback


async def test_delete_delegates_to_shared_service(monkeypatch, handler_env):
    subscription, callback = handler_env
    deleter = AsyncMock()
    monkeypatch.setattr('app.services.subscription_deletion_service.delete_subscription_record', deleter)
    db = AsyncMock()

    await my_subscriptions.handle_subscription_delete_execute(callback, SimpleNamespace(id=7), db, AsyncMock())

    deleter.assert_awaited_once()
    assert deleter.await_args.args == (db, subscription)
    assert deleter.await_args.kwargs == {'deleted_by': 'user:7'}
    callback.answer.assert_awaited_once_with('Подписка удалена', show_alert=True)
    my_subscriptions.show_my_subscriptions.assert_awaited_once()


async def test_open_grace_is_reported_not_swallowed(monkeypatch, handler_env):
    from app.services.grace_access_runtime import GraceAccessDeletionBlocked

    _subscription_obj, callback = handler_env
    monkeypatch.setattr(
        'app.services.subscription_deletion_service.delete_subscription_record',
        AsyncMock(side_effect=GraceAccessDeletionBlocked((99,))),
    )

    await my_subscriptions.handle_subscription_delete_execute(callback, SimpleNamespace(id=7), AsyncMock(), AsyncMock())

    callback.answer.assert_awaited_once()
    assert 'временный доступ' in callback.answer.await_args.args[0]
    my_subscriptions.show_my_subscriptions.assert_not_awaited()
