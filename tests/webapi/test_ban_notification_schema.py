from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.config import settings
from app.services.ban_notification_service import BanNotificationService
from app.webapi.schemas.ban_notifications import BanNotificationRequest


@pytest.mark.parametrize(
    'notification_type',
    ['revoke', 'torrent', 'hwid_limit', 'suspicious_destination', 'traffic_limit', 'manual'],
)
def test_typed_ban_notification_types_are_accepted(notification_type: str) -> None:
    request = BanNotificationRequest(
        notification_type=notification_type,
        user_identifier='user@example.com',
        username='user',
        ban_minutes=60,
        reason='Test reason',
    )

    assert request.notification_type == notification_type


def test_unknown_typed_ban_notification_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BanNotificationRequest(
            notification_type='unknown',
            user_identifier='user@example.com',
            username='user',
        )


@pytest.mark.parametrize(
    ('field', 'value'),
    [('ip_count', -1), ('limit', -1), ('ban_minutes', 0), ('ban_minutes', 10081)],
)
def test_invalid_numeric_values_are_rejected(field: str, value: int) -> None:
    payload = {
        'notification_type': 'punishment',
        'user_identifier': 'user@example.com',
        'username': 'user',
        'ip_count': 2,
        'limit': 1,
        'ban_minutes': 30,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        BanNotificationRequest(**payload)


@pytest.mark.asyncio
async def test_invalid_typed_ban_template_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    service = BanNotificationService()
    service._bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(service, '_find_user_by_identifier', AsyncMock(return_value=SimpleNamespace(telegram_id=1)))
    monkeypatch.setattr(settings, 'BAN_MSG_TORRENT', '{unexpected_variable}')

    success, _, telegram_id = await service.send_typed_ban_notification(
        db=AsyncMock(),
        user_identifier='user@example.com',
        username='user',
        notification_type='torrent',
        ban_minutes=60,
        reason='Torrent activity',
    )

    assert success is True
    assert telegram_id == 1
    assert 'Torrent activity' in service._bot.send_message.await_args.kwargs['text']


@pytest.mark.asyncio
async def test_unknown_typed_ban_type_returns_safe_error() -> None:
    service = BanNotificationService()
    service._bot = AsyncMock()

    success, message, telegram_id = await service.send_typed_ban_notification(
        db=AsyncMock(),
        user_identifier='user@example.com',
        username='user',
        notification_type='unknown',
        ban_minutes=60,
    )

    assert success is False
    assert message == 'Неизвестный тип бана: unknown'
    assert telegram_id is None
