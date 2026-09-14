"""Действия в Mini App должны попадать в таймлайн активности.

Жалоба: человек час назад работал в боте и обновлял ключ, а в разделе
«Активность» пусто. Разбор: поверхностей у пользователя три — кнопки бота
(пишет ButtonStatsMiddleware), кабинет (пишет зависимость авторизации) и
Mini App. Третья не писала НИЧЕГО: у неё своя авторизация по init_data, мимо
кабинетной зависимости, и ни одного места записи.

Отличать действие от чтения по HTTP-методу здесь нельзя: Mini App шлёт
init_data телом, поэтому чтения тоже POST. Поэтому список действий задан явно,
а сторож ниже требует, чтобы каждый маршрут был отнесён к действиям или к
чтениям — новый маршрут не проскочит молча.
"""

from __future__ import annotations

import re

import pytest

from app.config import settings
from app.services.user_action_log_service import (
    MINIAPP_ACTION_PATHS,
    MINIAPP_READ_PATHS,
    normalize_cabinet_path,
    should_log_miniapp_action,
)


def _router_paths() -> set[str]:
    from app.webapi.routes import miniapp

    paths = set()
    for route in miniapp.router.routes:
        path = re.sub(r'\{[^}]+\}', '{id}', route.path)
        paths.add(f'/miniapp{path}')
    return paths


def test_every_miniapp_route_is_classified():
    """Каждый маршрут Mini App отнесён либо к действиям, либо к чтениям."""
    classified = MINIAPP_ACTION_PATHS | MINIAPP_READ_PATHS
    unclassified = sorted(_router_paths() - classified)

    assert not unclassified, (
        'новые маршруты Mini App не отнесены ни к действиям, ни к чтениям: '
        f'{unclassified}. Допишите путь в MINIAPP_ACTION_PATHS или MINIAPP_READ_PATHS.'
    )


def test_classification_has_no_stale_paths():
    """В списках нет путей, которых у роутера больше нет."""
    stale = sorted((MINIAPP_ACTION_PATHS | MINIAPP_READ_PATHS) - _router_paths())

    assert not stale, f'пути исчезли из роутера, уберите их из списков: {stale}'


def test_actions_are_logged_and_reads_are_not(monkeypatch):
    """Покупка — действие, просмотр подписки — нет."""
    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)

    assert should_log_miniapp_action('/miniapp/subscription/purchase') is True
    assert should_log_miniapp_action('/miniapp/subscription/traffic-topup') is True
    assert should_log_miniapp_action('/miniapp/promo-offers/42/claim') is True

    assert should_log_miniapp_action('/miniapp/subscription') is False
    assert should_log_miniapp_action('/miniapp/subscription/tariffs') is False
    assert should_log_miniapp_action('/miniapp/subscription/purchase/preview') is False
    assert should_log_miniapp_action('/miniapp/payments/status') is False

    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', False, raising=False)
    assert should_log_miniapp_action('/miniapp/subscription/purchase') is False


def test_path_normalization_keeps_prefix():
    assert normalize_cabinet_path('/miniapp/promo-offers/42/claim') == '/miniapp/promo-offers/{id}/claim'


@pytest.mark.asyncio
async def test_authorize_writes_action_for_mutating_request(monkeypatch):
    """Авторизация запроса Mini App пишет действие в тот же журнал."""
    from datetime import UTC, datetime

    from app.database.models import Base, ButtonClickLog, User
    from app.services import user_action_log_service as log_module
    from app.webapi.routes import miniapp
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)
    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda init_data, token: {'user': {'id': 555000111}})

    written: list[dict] = []

    async def _capture(user_id, button_id, callback_data, button_type):
        written.append(
            {'user_id': user_id, 'button_id': button_id, 'callback_data': callback_data, 'type': button_type}
        )

    monkeypatch.setattr(log_module, '_write_action', _capture)

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(
            User(
                id=7,
                telegram_id=555000111,
                first_name='U',
                status='active',
                language='ru',
                balance_kopeks=0,
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

        token = log_module.bind_request_path('/miniapp/subscription/purchase')
        try:
            user = await miniapp._authorize_miniapp_user('stub', db)
        finally:
            log_module.reset_request_path(token)

        assert user.id == 7
        # Задача пишется в фоне — дождёмся её.
        await log_module.drain_pending_actions()

    assert written == [
        {
            'user_id': 7,
            'button_id': 'POST /miniapp/subscription/purchase',
            'callback_data': '/miniapp/subscription/purchase',
            'type': 'miniapp',
        }
    ]
    assert ButtonClickLog is not None


@pytest.mark.asyncio
async def test_authorize_writes_screen_for_reads(monkeypatch):
    """Просмотр экрана — тоже след: пишется как экран, а не как действие."""
    from datetime import UTC, datetime

    from app.database.models import Base, User
    from app.services import user_action_log_service as log_module
    from app.webapi.routes import miniapp
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(settings, 'USER_ACTION_LOG_ENABLED', True, raising=False)
    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda init_data, token: {'user': {'id': 555000111}})

    written: list[dict] = []

    async def _capture(user_id, button_id, callback_data, button_type):
        written.append({'user_id': user_id, 'button_id': button_id, 'type': button_type})

    monkeypatch.setattr(log_module, '_write_action', _capture)
    log_module._recent_screens.clear()

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(
            User(
                id=7,
                telegram_id=555000111,
                first_name='U',
                status='active',
                language='ru',
                balance_kopeks=0,
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

        token = log_module.bind_request_path('/miniapp/subscription')
        try:
            await miniapp._authorize_miniapp_user('stub', db)
        finally:
            log_module.reset_request_path(token)
        await log_module.drain_pending_actions()

    assert written == [{'user_id': 7, 'button_id': 'SCREEN /miniapp/subscription', 'type': 'miniapp'}]


@pytest.mark.asyncio
async def test_timeline_shows_miniapp_actions(monkeypatch):
    """Записанное действие Mini App видно в «Активности» и не смешано с ботом."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.cabinet.routes.admin_users import get_user_activity
    from app.database.models import Base, ButtonClickLog, User
    from tests.fixtures.sqlite_memory import memory_session

    admin = SimpleNamespace(id=1, telegram_id=1)
    now = datetime.now(UTC)

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(
            User(
                id=7,
                telegram_id=555000111,
                first_name='U',
                status='active',
                language='ru',
                balance_kopeks=0,
                created_at=now,
            )
        )
        db.add_all(
            [
                ButtonClickLog(
                    button_id='POST /miniapp/subscription/purchase',
                    user_id=7,
                    callback_data='/miniapp/subscription/purchase',
                    button_type='miniapp',
                    clicked_at=now,
                ),
                ButtonClickLog(
                    button_id='menu_subscription',
                    user_id=7,
                    callback_data='menu_subscription',
                    button_type='builtin',
                    clicked_at=now,
                ),
            ]
        )
        await db.commit()

        everything = await get_user_activity(user_id=7, offset=0, limit=50, types=None, admin=admin, db=db)
        only_miniapp = await get_user_activity(
            user_id=7, offset=0, limit=50, types='miniapp_action', admin=admin, db=db
        )

    kinds = {item.type for item in everything.items}
    assert kinds == {'miniapp_action', 'button_click'}, kinds
    assert everything.total == 2

    assert [item.type for item in only_miniapp.items] == ['miniapp_action']
    assert only_miniapp.items[0].source == 'miniapp'
    assert only_miniapp.items[0].title == 'POST /miniapp/subscription/purchase'
