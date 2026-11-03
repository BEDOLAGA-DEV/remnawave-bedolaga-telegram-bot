from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import user_service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'language, marker', [('ru', 'баланс'), ('en', 'balance'), ('ua', 'баланс'), ('zh', '余额'), ('fa', 'موجودی')]
)
@pytest.mark.parametrize('active', [False, True])
async def test_topup_text_and_actions_preserve_recipient_language(monkeypatch, language, marker, active):
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(user_service.notification_delivery_service, 'notify_balance_topup', notify)
    monkeypatch.setattr(type(user_service.settings), 'is_multi_tariff_enabled', lambda self: False)
    user = SimpleNamespace(language=language, balance_kopeks=50000)
    subscription = SimpleNamespace(status='active') if active else None
    await user_service.UserService().send_topup_success_to_user(None, user, 15000, subscription)
    data = notify.await_args.kwargs
    assert marker in data['telegram_message'].lower()
    assert data['amount_kopeks'] == 15000
    assert data['new_balance_kopeks'] == 50000
    callbacks = [button.callback_data for row in data['telegram_markup'].inline_keyboard for button in row]
    assert callbacks == (
        ['subscription_extend'] if active else ['subscription_buy', 'subscription_extend', 'subscription_add_devices']
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('language, marker', [('en', 'balance'), ('zh', '余额'), ('fa', 'موجودی')])
@pytest.mark.parametrize('amount', [-15000, 15000])
async def test_balance_adjustment_keeps_amount_and_hides_admin(monkeypatch, language, marker, amount):
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(user_service.notification_delivery_service, 'send_notification', notify)
    user = SimpleNamespace(language=language, balance_kopeks=50000, subscriptions=[])
    await user_service.UserService()._send_balance_notification(None, user, amount, 'PRIVATE_ADMIN_NAME')
    data = notify.await_args.kwargs
    assert marker in data['telegram_message'].lower()
    assert 'PRIVATE_ADMIN_NAME' not in data['telegram_message']
    assert data['context']['amount_kopeks'] == amount
    assert data['context']['new_balance_kopeks'] == 50000
