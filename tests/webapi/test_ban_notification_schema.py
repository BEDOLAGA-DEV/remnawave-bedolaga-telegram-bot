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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('method', 'kwargs', 'field'),
    [
        ('send_punishment_notification', {'ip_count': 3, 'limit': 1, 'ban_minutes': 30}, 'node_name'),
        ('send_network_wifi_notification', {'ban_minutes': 30}, 'node_name'),
        ('send_network_wifi_notification', {'ban_minutes': 30}, 'network_type'),
        ('send_network_mobile_notification', {'ban_minutes': 30}, 'node_name'),
        ('send_network_mobile_notification', {'ban_minutes': 30}, 'network_type'),
    ],
)
async def test_external_values_are_escaped_before_html_send(
    monkeypatch: pytest.MonkeyPatch, method: str, kwargs: dict, field: str
) -> None:
    """Имя ноды и тип сети приходят снаружи и уезжают в сообщение с parse_mode=HTML.

    Неэкранированная угловая скобка делает разметку невалидной, Telegram
    отвечает 400, и уведомление о бане не доходит вообще — то есть человек не
    узнаёт, почему у него пропал доступ.
    """
    service = BanNotificationService()
    service._bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(service, '_find_user_by_identifier', AsyncMock(return_value=SimpleNamespace(telegram_id=1)))

    success, _, _ = await getattr(service, method)(
        db=AsyncMock(),
        user_identifier='user@example.com',
        username='user',
        **{**kwargs, field: 'Node <b>EU</b> & Co'},
    )

    assert success is True
    text = service._bot.send_message.await_args.kwargs['text']
    assert 'Node &lt;b&gt;EU&lt;/b&gt; &amp; Co' in text
    assert '<b>EU</b>' not in text


@pytest.mark.asyncio
async def test_typed_ban_reason_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Причина бана тоже приходит снаружи — экранируем."""
    service = BanNotificationService()
    service._bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(service, '_find_user_by_identifier', AsyncMock(return_value=SimpleNamespace(telegram_id=1)))

    await service.send_typed_ban_notification(
        db=AsyncMock(),
        user_identifier='user@example.com',
        username='user',
        notification_type='torrent',
        ban_minutes=60,
        reason='<script>alert(1)</script>',
    )

    text = service._bot.send_message.await_args.kwargs['text']
    assert '&lt;script&gt;' in text
    assert '<script>' not in text


@pytest.mark.asyncio
async def test_warning_text_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Текст предупреждения приходит по API и не должен ломать разметку."""
    service = BanNotificationService()
    service._bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(service, '_find_user_by_identifier', AsyncMock(return_value=SimpleNamespace(telegram_id=1)))

    await service.send_warning_notification(
        db=AsyncMock(),
        user_identifier='user@example.com',
        username='user',
        warning_message='Не используйте <torrent> & p2p',
    )

    text = service._bot.send_message.await_args.kwargs['text']
    assert '&lt;torrent&gt; &amp; p2p' in text


@pytest.mark.asyncio
async def test_revoke_uses_its_own_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """revoke — это сброс ключей, а не бан: текст должен отличаться от punishment."""
    service = BanNotificationService()
    service._bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(service, '_find_user_by_identifier', AsyncMock(return_value=SimpleNamespace(telegram_id=1)))

    await service.send_punishment_notification(
        db=AsyncMock(),
        user_identifier='user@example.com',
        username='user',
        ip_count=3,
        limit=1,
        ban_minutes=30,
        revoke=True,
    )
    revoke_text = service._bot.send_message.await_args.kwargs['text']

    await service.send_punishment_notification(
        db=AsyncMock(),
        user_identifier='user@example.com',
        username='user',
        ip_count=3,
        limit=1,
        ban_minutes=30,
        revoke=False,
    )
    ban_text = service._bot.send_message.await_args.kwargs['text']

    assert revoke_text != ban_text
    assert 'КЛЮЧИ ДОСТУПА ОБНОВЛЕНЫ' in revoke_text
    assert 'ЗАБЛОКИРОВАН' in ban_text


@pytest.mark.asyncio
async def test_unexpected_error_returns_500_not_typeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неожиданная ошибка обязана превращаться в 500, а не в TypeError.

    В прежнем коде стояло `logger.exception(...)(status_code=..., detail=...)` —
    результат логгера (None) вызывался как функция, поэтому вместо ответа
    клиент получал TypeError изнутри обработчика ошибок.
    """
    from fastapi import HTTPException

    from app.webapi.routes import ban_notifications as route_module

    monkeypatch.setattr(
        route_module.ban_notification_service,
        'send_warning_notification',
        AsyncMock(side_effect=RuntimeError('boom')),
    )

    payload = BanNotificationRequest(
        notification_type='warning',
        user_identifier='user@example.com',
        username='user',
        warning_message='hi',
    )

    with pytest.raises(HTTPException) as exc:
        await route_module.send_ban_notification(payload, db=AsyncMock(), _token=None)

    assert exc.value.status_code == 500
