from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import monitoring_service


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('kind', ['expired', 'trial', 'success', 'failed'])
async def test_monitoring_recipient_text_escapes_tariff_and_keeps_actions(monkeypatch, language, kind):
    monkeypatch.setattr(type(monitoring_service.settings), 'is_multi_tariff_enabled', lambda self: True)
    service = monitoring_service.MonitoringService.__new__(monitoring_service.MonitoringService)
    service._send_message_with_logo = AsyncMock()
    user = SimpleNamespace(id=1, telegram_id=123, language=language)
    subscription = SimpleNamespace(id=7, tariff=SimpleNamespace(name='Plan <test> & name'))
    if kind == 'expired':
        assert await service._send_subscription_expired_notification(user, subscription)
    elif kind == 'trial':
        assert await service._send_trial_ending_notification(user, subscription)
    elif kind == 'success':
        await service._send_autopay_success_notification(user, 15000, 30, subscription=subscription)
    else:
        await service._send_autopay_failed_notification(user, 1000, 15000, subscription=subscription)
    sent = service._send_message_with_logo.await_args.kwargs
    assert sent['chat_id'] == 123
    assert 'Plan &lt;test&gt; &amp; name' in sent['text']
    if language in {'en', 'zh', 'fa'}:
        assert 'Тариф:' not in sent['text']
        assert 'Ваша подписка' not in sent['text']
        assert 'Тестовая подписка' not in sent['text']
    if kind in {'expired', 'trial', 'failed'}:
        callbacks = [button.callback_data for row in sent['reply_markup'].inline_keyboard for button in row]
        assert 'balance_topup' in callbacks
