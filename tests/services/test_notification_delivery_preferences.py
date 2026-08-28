from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import UserStatus
from app.services.notification_delivery_service import NotificationDeliveryService, NotificationType


def _user(**notification_settings):
    return SimpleNamespace(
        id=42,
        status=UserStatus.ACTIVE.value,
        telegram_id=4242,
        email=None,
        email_verified=False,
        notification_settings=notification_settings,
    )


async def _send(service, user, notification_type):
    return await service.send_notification(
        user=user,
        notification_type=notification_type,
        context={},
        bot=SimpleNamespace(),
        telegram_message='test',
    )


async def test_global_switch_blocks_regular_notifications(monkeypatch):
    service = NotificationDeliveryService()
    service._send_telegram_notification = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', False)

    result = await _send(service, _user(), NotificationType.BALANCE_TOPUP)

    assert result is False
    service._send_telegram_notification.assert_not_awaited()


@pytest.mark.parametrize(
    'notification_type',
    [
        NotificationType.EMAIL_VERIFICATION,
        NotificationType.PASSWORD_RESET,
        NotificationType.GUEST_SUBSCRIPTION_DELIVERED,
    ],
)
async def test_transactional_messages_bypass_global_switch(monkeypatch, notification_type):
    service = NotificationDeliveryService()
    service._send_telegram_notification = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', False)

    result = await _send(service, _user(), notification_type)

    assert result is True
    service._send_telegram_notification.assert_awaited_once()


@pytest.mark.parametrize(
    ('notification_type', 'preference'),
    [
        (NotificationType.SUBSCRIPTION_EXPIRING, 'subscription_expiry_enabled'),
        (NotificationType.SUBSCRIPTION_EXPIRED, 'subscription_expiry_enabled'),
        (NotificationType.WEBHOOK_SUB_EXPIRING, 'subscription_expiry_enabled'),
        (NotificationType.WINBACK_DISCOUNT, 'subscription_expiry_enabled'),
        (NotificationType.BALANCE_LOW, 'balance_low_enabled'),
        (NotificationType.WEBHOOK_SUB_BANDWIDTH_THRESHOLD, 'traffic_warning_enabled'),
        (NotificationType.PROMO_OFFER, 'promo_offers_enabled'),
    ],
)
async def test_per_user_preferences_block_matching_notifications(monkeypatch, notification_type, preference):
    service = NotificationDeliveryService()
    service._send_telegram_notification = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)

    result = await _send(service, _user(**{preference: False}), notification_type)

    assert result is False
    service._send_telegram_notification.assert_not_awaited()


async def test_referral_switch_is_enforced_by_unified_delivery(monkeypatch):
    service = NotificationDeliveryService()
    service._send_telegram_notification = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', False)

    result = await _send(service, _user(), NotificationType.REFERRAL_BONUS)

    assert result is False
    service._send_telegram_notification.assert_not_awaited()


async def test_blocked_user_can_receive_the_ban_notification(monkeypatch):
    service = NotificationDeliveryService()
    service._send_telegram_notification = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    user = _user()
    user.status = UserStatus.BLOCKED.value

    result = await _send(service, user, NotificationType.BAN_NOTIFICATION)

    assert result is True
    service._send_telegram_notification.assert_awaited_once()


async def test_blocked_user_does_not_receive_unrelated_notifications(monkeypatch):
    service = NotificationDeliveryService()
    service._send_telegram_notification = AsyncMock(return_value=True)
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    user = _user()
    user.status = UserStatus.BLOCKED.value

    result = await _send(service, user, NotificationType.BALANCE_TOPUP)

    assert result is False
    service._send_telegram_notification.assert_not_awaited()


async def test_promo_opt_out_suppresses_the_message_but_keeps_the_discount(monkeypatch):
    """Отказ от промо-уведомлений глушит СООБЩЕНИЕ, но не отбирает скидку.

    Гейт стоял выше создания оффера, поэтому отказ от уведомлений лишал человека
    самой скидки: строки `DiscountOffer` не появлялось, и в кабинете с миниаппой
    предложение просто отсутствовало. Кабинетная рассылка того же PR ведёт себя
    ровно наоборот (создаёт оффер всем, фильтрует только адресатов уведомления),
    так что одна и та же кампания расходилась в зависимости от того, из какой
    админки её запустили. Плюс отказавшиеся попадали в отчёт админа как ОШИБКИ.
    """
    import ast
    import inspect

    import app.handlers.admin.promo_offers as mod

    src = inspect.getsource(mod)
    tree = ast.parse(src)

    func = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == 'send_single_offer'
    )

    def _line_of(predicate) -> int:
        return min(node.lineno for node in ast.walk(func) if predicate(node))

    upsert_line = _line_of(
        lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'upsert_discount_offer'
    )
    gate_line = _line_of(
        lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'is_promo_offers_enabled'
    )

    assert gate_line > upsert_line, (
        'проверка промо-настройки снова стоит выше создания оффера — '
        'отказ от уведомлений отберёт у человека саму скидку'
    )
