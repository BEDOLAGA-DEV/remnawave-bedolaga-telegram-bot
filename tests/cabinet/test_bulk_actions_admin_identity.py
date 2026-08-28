"""Массовое действие не должно падать целиком из-за отката внутри цикла.

``admin`` приходит из зависимости ``require_permission`` и живёт в ТОЙ ЖЕ
сессии, что и ``db`` (FastAPI кеширует ``get_cabinet_db`` на запрос). Любой
откат внутри цикла экспайрит все объекты сессии — включая самого админа, —
и следующее чтение ``admin.id`` роняет MissingGreenlet уже вне try/except,
то есть весь запрос уходит в 500. Часть элементов пакета к этому моменту
уже закоммичена, и админ об этом не узнаёт.

Откат внутри цикла — штатный сценарий, а не авария:
``ensure_no_open_grace_for_subscriptions`` делает ``db.rollback()`` перед
тем, как сообщить «подписка под grace-доступом», и обработчики ошибок
``_execute_for_*`` тоже откатывают сессию.

Проверено на SQLAlchemy 2.0 + aiosqlite: после ``rollback()`` даже чтение
первичного ключа поднимает MissingGreenlet, и ``expire_on_commit=False``
от этого не спасает — откат экспайрит объекты независимо от этой настройки.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import MissingGreenlet

import app.cabinet.routes.admin_bulk_actions as bulk
from app.cabinet.schemas.bulk_actions import BulkActionParams, BulkActionType, BulkExecuteRequest, BulkUserResult


class _ExpiringAdmin:
    """Админ, у которого атрибуты протухают после отката сессии."""

    def __init__(self, admin_id: int = 7) -> None:
        self._id = admin_id
        self.expired = False

    @property
    def id(self) -> int:
        if self.expired:
            raise MissingGreenlet('greenlet_spawn has not been called')
        return self._id


@pytest.mark.asyncio
async def test_subscription_batch_survives_rollback_inside_loop(monkeypatch):
    """Откат на первой подписке не должен уносить весь ответ в 500."""
    admin = _ExpiringAdmin()
    processed: list[int] = []

    async def fake_execute(db, sid, action, params, tariff, dry_run):
        processed.append(sid)
        # первая подписка под grace-доступом: guard откатил сессию
        admin.expired = True
        return BulkUserResult(user_id=1, subscription_id=sid, success=sid != 10, message='ok')

    monkeypatch.setattr(bulk, '_execute_for_subscription', fake_execute)

    response = await bulk.bulk_execute(
        BulkExecuteRequest(
            action=BulkActionType.DELETE_SUBSCRIPTION,
            subscription_ids=[10, 11],
            params=BulkActionParams(),
        ),
        stream=False,
        admin=admin,
        db=MagicMock(),
    )

    assert processed == [10, 11]
    assert response.success_count == 1
    assert response.error_count == 1


@pytest.mark.asyncio
async def test_user_batch_survives_rollback_inside_loop(monkeypatch):
    """То же для режима по пользователям: id админа читается на каждой итерации."""
    admin = _ExpiringAdmin()
    seen_admin_ids: list[int] = []

    async def fake_execute(db, uid, action, params, tariff, dry_run, admin_id=0):
        seen_admin_ids.append(admin_id)
        # обработчик поймал ошибку и откатил сессию
        admin.expired = True
        return BulkUserResult(user_id=uid, success=True, message='ok')

    monkeypatch.setattr(bulk, '_execute_for_user', fake_execute)

    response = await bulk.bulk_execute(
        BulkExecuteRequest(
            action=BulkActionType.EXTEND_SUBSCRIPTION,
            user_ids=[1, 2],
            params=BulkActionParams(days=5),
        ),
        stream=False,
        admin=admin,
        db=MagicMock(),
    )

    assert response.success_count == 2
    # снимок сделан один раз до цикла, поэтому обе итерации видят один и тот же id
    assert seen_admin_ids == [7, 7]


@pytest.mark.asyncio
async def test_streamed_batch_survives_rollback_inside_loop(monkeypatch):
    """SSE-поток не должен обрываться на финальном логе после отката."""
    admin = _ExpiringAdmin()

    async def fake_execute(db, uid, action, params, tariff, dry_run, admin_id=0):
        admin.expired = True
        return BulkUserResult(user_id=uid, success=True, message='ok')

    monkeypatch.setattr(bulk, '_execute_for_user', fake_execute)

    response = await bulk.bulk_execute(
        BulkExecuteRequest(
            action=BulkActionType.EXTEND_SUBSCRIPTION,
            user_ids=[1, 2],
            params=BulkActionParams(days=5),
        ),
        stream=True,
        admin=admin,
        db=MagicMock(),
    )

    events = [chunk async for chunk in response.body_iterator]

    assert len(events) == 3  # два progress + итоговый complete
    assert '"type": "complete"' in events[-1]


@pytest.mark.asyncio
async def test_delete_user_permission_check_runs_before_snapshot(monkeypatch):
    """Снимок id не должен обгонять проверку прав: отказ обязан остаться отказом."""
    admin = _ExpiringAdmin()

    async def deny(db, user, permission):
        return False, 'no'

    monkeypatch.setattr(
        'app.services.permission_service.PermissionService.check_permission',
        AsyncMock(side_effect=deny),
    )

    with pytest.raises(Exception) as exc:
        await bulk.bulk_execute(
            BulkExecuteRequest(
                action=BulkActionType.DELETE_USER,
                user_ids=[1],
                params=BulkActionParams(),
            ),
            stream=False,
            admin=admin,
            db=MagicMock(),
        )

    assert getattr(exc.value, 'status_code', None) == 403


def test_stream_helpers_take_plain_admin_id():
    """Генераторы принимают int, а не ORM-объект: протухать в них нечему."""
    import inspect

    for func in (bulk._stream_bulk_execute, bulk._stream_bulk_execute_subscriptions):
        params = inspect.signature(func).parameters
        assert 'admin' not in params
        assert params['admin_id'].annotation is int
