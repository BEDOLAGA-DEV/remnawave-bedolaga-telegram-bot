from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.services import nalogo_queue_service as queue_module, traffic_monitoring_service as traffic_module


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('check_type', ['fast', 'daily', 'manual'])
async def test_actual_traffic_notice_localizes_without_changing_threshold_or_cooldown(
    monkeypatch, language, check_type
):
    sent = AsyncMock(return_value=True)
    admin = SimpleNamespace(
        _notification_texts=AsyncMock(return_value=get_texts(language)), send_suspicious_traffic_notification=sent
    )
    monkeypatch.setattr(traffic_module, 'AdminNotificationService', lambda bot: admin)
    monkeypatch.setattr(settings, 'SUSPICIOUS_NOTIFICATIONS_TOPIC_ID', None)
    monkeypatch.setattr(traffic_module, 'AsyncSessionLocal', Session)
    monkeypatch.setattr(
        traffic_module,
        'get_user_by_remnawave_id',
        AsyncMock(
            return_value=SimpleNamespace(
                telegram_id=None, email='sample@receipt.example', id=9, full_name='<Example>', username='sample'
            )
        ),
    )
    service = traffic_module.TrafficMonitoringServiceV2()
    service.should_send_notification = AsyncMock(return_value=True)
    service.record_notification = AsyncMock()
    violation = traffic_module.TrafficViolation(
        7, 123, 'unused', None, 125.75, 100.25, 'masked-1', '<Node> & operator', check_type
    )
    bot = object()
    await service._send_violation_notifications([violation], bot)
    message = sent.await_args.args[0]
    expected = get_texts(language).t('TRAFFIC_NOTICE_BODY', '')
    assert expected
    assert expected.split('\n')[0] in message
    assert '25.50' in message and '125.75' in message and '100.25' in message
    assert '&lt;Node&gt; &amp; operator' in message and '&lt;Example&gt;' in message
    assert sent.await_args.args[1:] == (bot, None)
    service.record_notification.assert_awaited_once_with(7)
    service.should_send_notification.return_value = False
    sent.reset_mock()
    await service._send_violation_notifications([violation], bot)
    sent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('topic', [None, 77])
async def test_actual_queue_notification_language_and_existing_cooldown(monkeypatch, language, topic):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'en')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '123')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', topic)
    monkeypatch.setattr('app.database.database.AsyncSessionLocal', Session)
    lookup = AsyncMock(return_value=SimpleNamespace(language=language))
    monkeypatch.setattr('app.database.crud.user.get_user_by_telegram_id', lookup)
    service = queue_module.NalogoQueueService()
    service._bot = SimpleNamespace(send_message=AsyncMock())
    key = 'NALOGO_QUEUE_CLEARED'
    await service._send_admin_notification(
        'fallback', localization_key=key, localization_values={'count': 2, 'amount': 1234.56}
    )
    text = get_texts('en' if topic else language).t(key, '')
    assert text
    args = service._bot.send_message.await_args.kwargs
    assert args['text'] == text.format(count=2, amount=1234.56)
    assert args['chat_id'] == 123 and args['message_thread_id'] == topic
    assert args['parse_mode'] == 'HTML'
    if topic:
        lookup.assert_not_awaited()
    await service._send_admin_notification('next')
    service._bot.send_message.assert_awaited_once()
    await service._send_admin_notification('Operator <b>content</b>', skip_cooldown=True)
    assert service._bot.send_message.await_args.kwargs['text'] == 'Operator <b>content</b>'
