"""Глобальный выключатель уведомлений обязан гасить и BanHammer-сообщения.

Пять методов ``BanNotificationService`` выходят на ``is_notifications_enabled()``
сразу после проверки бота, а типизированные баны (torrent, hwid_limit,
suspicious_destination, traffic_limit, manual) этой проверки не имели: с
выключенными уведомлениями сообщение всё равно уходило в Telegram. Причём
email-ветка того же метода уходит через ``notification_delivery_service``,
который рубильник уважает, — то есть одна и та же настройка работала для
почты и не работала для Telegram.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.ban_notification_service import BanNotificationService


TYPED_BANS = ['torrent', 'hwid_limit', 'suspicious_destination', 'traffic_limit', 'manual']


@pytest.fixture
def service(monkeypatch) -> BanNotificationService:
    instance = BanNotificationService()
    instance._bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(
        instance,
        '_find_user_by_identifier',
        AsyncMock(return_value=SimpleNamespace(id=1, telegram_id=148871030, username='client')),
    )
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize('notification_type', TYPED_BANS)
async def test_typed_ban_is_silent_when_notifications_are_off(
    monkeypatch, service: BanNotificationService, notification_type: str
) -> None:
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: False)

    success, message, telegram_id = await service.send_typed_ban_notification(
        db=AsyncMock(),
        user_identifier='client',
        username='client',
        notification_type=notification_type,
        ban_minutes=60,
    )

    assert success is False
    assert telegram_id is None
    assert 'отключены' in message
    service._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_ban_is_delivered_when_notifications_are_on(monkeypatch, service: BanNotificationService) -> None:
    """Обратная сторона: рубильник не должен глушить включённые уведомления."""
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'BAN_MSG_TORRENT', 'Торренты запрещены: {reason}', raising=False)

    success, _, telegram_id = await service.send_typed_ban_notification(
        db=AsyncMock(),
        user_identifier='client',
        username='client',
        notification_type='torrent',
        ban_minutes=60,
    )

    assert success is True
    assert telegram_id == 148871030
    service._bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_is_checked_before_touching_the_database(monkeypatch, service: BanNotificationService) -> None:
    """Выход обязан быть до поиска пользователя, как у соседних методов.

    Иначе выключенные уведомления всё равно ходят в БД на каждый бан.
    """
    monkeypatch.setattr(type(settings), 'is_notifications_enabled', lambda self: False)

    await service.send_typed_ban_notification(
        db=AsyncMock(),
        user_identifier='client',
        username='client',
        notification_type='manual',
        ban_minutes=60,
    )

    service._find_user_by_identifier.assert_not_awaited()


def test_every_send_method_respects_the_switch() -> None:
    """Ни один способ уведомить пользователя не должен обходить рубильник.

    Проверка по исходнику: следующий добавленный метод так же тихо разойдётся
    с остальными, и заметить это можно будет только в проде.
    """
    offenders = []
    for name, method in inspect.getmembers(BanNotificationService, inspect.isfunction):
        if not name.startswith('send_'):
            continue
        if 'is_notifications_enabled' not in inspect.getsource(method):
            offenders.append(name)

    assert offenders == [], f'методы отправки без проверки рубильника: {offenders}'
