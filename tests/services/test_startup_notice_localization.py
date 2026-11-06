"""Startup/shutdown diagnostics use the recipient locale, keeping raw diagnostics intact."""

import asyncio
import signal
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.services import startup_notification_service as module
from tests.services.test_startup_notification_summary import _assert_valid_telegram_html, _stats


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_renderers_preserve_numeric_values_and_escape_custom_status(language):
    texts = get_texts(language)
    stats = _stats(panel_status='<operator>&', panel_connected=False, open_tickets=7, maintenance=True)
    classic = module.render_startup_message(stats, timestamp='<time>', texts=texts)
    rich = module.render_startup_rich(stats, texts=texts)
    _assert_valid_telegram_html(classic)
    for content in (classic, rich):
        assert texts.t('STARTUP_NOTICE_022') in content
        assert texts.t('STARTUP_NOTICE_019') in content
        assert texts.t('STARTUP_NOTICE_020') in content
        assert texts.t('STARTUP_NOTICE_021').format(v0='7') in content
        assert '&lt;operator&gt;&amp;' in content
        assert '4 558' in content and '24 012 ₽' in content
        assert texts.t('STARTUP_NOTICE_002').format(v0=142) in content
    assert module.format_uptime(timedelta(days=3, hours=4, minutes=12), texts) == ' '.join(
        [
            texts.t('STARTUP_NOTICE_055').format(v0=3),
            texts.t('STARTUP_NOTICE_056').format(v0=4),
            texts.t('STARTUP_NOTICE_057').format(v0=12),
        ]
    )
    for reason in [
        module.ShutdownReason(signum=signal.SIGTERM),
        module.ShutdownReason(error=ValueError('<raw>&'), source='main_loop'),
    ]:
        shutdown = module.render_shutdown_message(
            reason, version='test', started_at=None, now=datetime(2026, 1, 1, tzinfo=UTC), texts=texts
        )
        _assert_valid_telegram_html(shutdown)
        if reason.is_failure:
            assert texts.t('STARTUP_NOTICE_001') in shutdown
            assert '&lt;raw&gt;&amp;' in shutdown
        else:
            assert texts.t('STARTUP_NOTICE_010') in shutdown


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.asyncio
async def test_shared_senders_use_configured_locale_and_keep_crash_file(monkeypatch, language):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', language)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-123')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID', 42)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', 43)
    session = AsyncMock()
    monkeypatch.setattr(module, 'AsyncSessionLocal', session)
    from app.utils import rich_admin

    monkeypatch.setattr(rich_admin, 'try_send_rich_admin_message', AsyncMock(return_value=False))
    service = module.StartupNotificationService(SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock()))
    monkeypatch.setattr(service, '_collect_stats', AsyncMock(return_value=_stats()))
    assert await service.send_startup_notification()
    sent = service.bot.send_message.await_args.kwargs
    texts = get_texts(language)
    assert texts.t('STARTUP_NOTICE_003') in sent['text']
    assert sent['message_thread_id'] == 42
    assert sent['reply_markup'].inline_keyboard[0][0].text == texts.t('STARTUP_NOTICE_084')
    assert await module.send_crash_notification(service.bot, ValueError('Redis <raw>&'), 'traceback unchanged')
    crash = service.bot.send_document.await_args.kwargs
    assert texts.t('STARTUP_NOTICE_041') in crash['caption']
    assert 'Redis &lt;raw&gt;&amp;' in crash['caption']
    assert crash['message_thread_id'] == 43
    assert crash['reply_markup'].inline_keyboard[0][0].text == texts.t('STARTUP_NOTICE_083')
    assert 'Error Message: Redis <raw>&' in crash['document'].data.decode()
    assert 'traceback unchanged' in crash['document'].data.decode()
    assert await module.send_shutdown_notification(
        service.bot, module.ShutdownReason(error=ValueError('<raw>')), started_at=None
    )
    stopped = service.bot.send_message.await_args.kwargs
    assert texts.t('STARTUP_NOTICE_058') in stopped['text']
    assert stopped['message_thread_id'] == 43
    session.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_private_recipients_and_unknown_fallback(monkeypatch):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'zh')
    monkeypatch.setattr(settings, 'ADMIN_IDS', '101,102')
    from app.services.support_settings_service import SupportSettingsService

    monkeypatch.setattr(SupportSettingsService, 'is_moderator', lambda chat: False)
    queries = []

    async def scalar(query):
        recipient = query.compile().params['telegram_id_1']
        queries.append(recipient)
        await asyncio.sleep(0)
        return {101: 'en', 102: 'fa'}[recipient]

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(scalar=scalar)

    monkeypatch.setattr(module, 'AsyncSessionLocal', session)
    texts = await asyncio.gather(
        module._recipient_texts(101, None),
        module._recipient_texts(102, None),
        module._recipient_texts(999, None),
        module._recipient_texts(101, 42),
    )
    assert [t.language for t in texts] == ['en', 'fa', 'zh', 'zh']
    assert sorted(queries) == [101, 102]
