from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import manual_topup_service
from app.services.payment import common


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'language, title',
    [
        ('ru', 'Баланс пополнен'),
        ('en', 'Balance topped up'),
        ('ua', 'Баланс поповнено'),
        ('zh', '余额已充值'),
        ('fa', 'موجودی افزایش یافت'),
    ],
)
async def test_manual_topup_notice_uses_recipient_language(monkeypatch, language, title):
    monkeypatch.setattr(type(manual_topup_service.settings), 'is_notifications_enabled', lambda self: True)
    keyboard = AsyncMock(return_value=None)
    email = AsyncMock()
    monkeypatch.setattr(common.PaymentCommonMixin, 'build_topup_success_keyboard', keyboard)
    monkeypatch.setattr(common, 'notify_email_user_topup', email)
    user = SimpleNamespace(id=1, telegram_id=123, language=language, balance_kopeks=50000)
    bot = SimpleNamespace(send_message=AsyncMock())
    await manual_topup_service._notify_user(user, 15000, SimpleNamespace(id=42), bot=bot)
    args = bot.send_message.await_args
    assert args.args[0] == 123
    assert title in args.args[1]
    assert manual_topup_service.settings.format_price(15000) in args.args[1]
    assert manual_topup_service.settings.format_price(50000) in args.args[1]
    assert '42' in args.args[1]
    email.assert_awaited_once_with(user, 15000)
    keyboard.assert_awaited_once_with(user)
