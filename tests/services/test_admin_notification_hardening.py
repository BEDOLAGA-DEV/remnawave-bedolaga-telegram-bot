"""Tests for the RemnaWave node-webhook flood-control fix.

Covers commits 3756ad66 + 0461279e:
- TelegramRetryAfter retry loop in AdminNotificationService._send_message
- bot-token redaction helper
- node-event coalescing buffer + overflow accounting
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramRetryAfter

from app.services.admin_notification_service import (
    AdminNotificationService,
    NotificationCategory,
    _redact_telegram_secrets,
)
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


# ---------------------------------------------------------------------------
# _redact_telegram_secrets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'expected_contains'),
    [
        (
            'connect to https://api.telegram.org/bot8123456789:AAH-Sample_TokenString_With-Chars-aiogram01/sendMessage',
            'bot[REDACTED]/sendMessage',
        ),
        (
            'bare token leak 123456789:AAHabcdefABCDEF0123456789zZxY1234',
            'bot[REDACTED]',
        ),
        (
            'token trailing dash 123456789:AAHabcdefABCDEF0123456789zZxY1234-',
            'bot[REDACTED]',
        ),
        (
            'token trailing underscore 123456789:AAHabcdefABCDEF0123456789zZxY1234_',
            'bot[REDACTED]',
        ),
        ('no token here at all', 'no token here at all'),
    ],
)
def test_redact_telegram_secrets(raw: str, expected_contains: str) -> None:
    redacted = _redact_telegram_secrets(raw)
    assert expected_contains in redacted
    # Sanity: no token-shape leaks survived
    assert '123456789:AAH' not in redacted
    assert '8123456789:AAH' not in redacted


def test_redact_telegram_secrets_handles_multiple_tokens() -> None:
    text = 'first 123456789:AAHabcdefABCDEF0123456789zZxY1234 second bot987654321:XYZabcdefABCDEF0123456789zZxY9876 end'
    redacted = _redact_telegram_secrets(text)
    assert redacted.count('bot[REDACTED]') == 2
    assert 'AAH' not in redacted
    assert 'XYZ' not in redacted


# ---------------------------------------------------------------------------
# AdminNotificationService._send_message — flood-control retry
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_service(monkeypatch: pytest.MonkeyPatch) -> AdminNotificationService:
    bot = MagicMock()
    service = AdminNotificationService(bot)
    # Enable the service and pin chat_id so the message-send path is reached.
    service.chat_id = -100123456
    service.enabled = True
    return service


@pytest.mark.asyncio
async def test_send_message_retries_on_flood_control(
    admin_service: AdminNotificationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RetryAfter on attempt 1, success on attempt 2 → exactly one sleep, returns True."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr('app.services.admin_notification_service.asyncio.sleep', fake_sleep)

    flood_error = TelegramRetryAfter(method=SimpleNamespace(), message='flood', retry_after=3)
    admin_service.bot.send_message = AsyncMock(side_effect=[flood_error, None])

    result = await admin_service._send_message('hello', category=NotificationCategory.INFRASTRUCTURE)

    assert result is True
    assert admin_service.bot.send_message.await_count == 2
    assert sleeps == [3]


@pytest.mark.asyncio
async def test_send_message_gives_up_after_three_flood_errors(
    admin_service: AdminNotificationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three consecutive RetryAfter → two sleeps, third attempt returns False without sleeping."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr('app.services.admin_notification_service.asyncio.sleep', fake_sleep)

    flood = TelegramRetryAfter(method=SimpleNamespace(), message='flood', retry_after=2)
    admin_service.bot.send_message = AsyncMock(side_effect=[flood, flood, flood])

    result = await admin_service._send_message('hi', category=NotificationCategory.INFRASTRUCTURE)

    assert result is False
    assert admin_service.bot.send_message.await_count == 3
    assert sleeps == [2, 2]  # third attempt does NOT sleep before returning False


@pytest.mark.asyncio
async def test_send_message_caps_retry_after_at_30s(
    admin_service: AdminNotificationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retry_after=120 from Telegram must be clamped to 30s to avoid blocking the flush task."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr('app.services.admin_notification_service.asyncio.sleep', fake_sleep)

    flood = TelegramRetryAfter(method=SimpleNamespace(), message='flood', retry_after=120)
    admin_service.bot.send_message = AsyncMock(side_effect=[flood, None])

    result = await admin_service._send_message('hi', category=NotificationCategory.INFRASTRUCTURE)

    assert result is True
    assert sleeps == [30]


# ---------------------------------------------------------------------------
# RemnaWaveWebhookService node-event coalescing
# ---------------------------------------------------------------------------


@pytest.fixture
def webhook_service() -> RemnaWaveWebhookService:
    bot = MagicMock()
    service = RemnaWaveWebhookService(bot)
    # Enable admin notifications so the flush path actually sends.
    service._admin_service.chat_id = -100123456
    service._admin_service.enabled = True
    service._admin_service.bot.send_message = AsyncMock(return_value=None)
    return service


@pytest.mark.asyncio
async def test_node_event_coalescing_keeps_one_flush_task(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """7 concurrent enqueues land in one buffer with one scheduled flush task."""
    for i in range(7):
        await webhook_service._enqueue_node_event(
            'node.connection_lost', {'name': f'node-{i}', 'address': f'10.0.0.{i}'}
        )

    bucket = webhook_service._node_event_buffer['node.connection_lost']
    assert len(bucket) == 7
    assert webhook_service._node_event_flush_task is not None
    assert not webhook_service._node_event_flush_task.done()

    webhook_service._node_event_flush_task.cancel()
    try:
        await webhook_service._node_event_flush_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_node_event_buffer_overflow_counts_dropped_events(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """Past BUFFER_MAX, events are dropped but counted in overflow."""
    cap = webhook_service._NODE_EVENT_BUFFER_MAX
    for i in range(cap + 3):
        await webhook_service._enqueue_node_event(
            'node.connection_lost', {'name': f'node-{i}', 'address': f'10.0.0.{i}'}
        )

    assert len(webhook_service._node_event_buffer['node.connection_lost']) == cap
    assert webhook_service._node_event_overflow['node.connection_lost'] == 3

    if webhook_service._node_event_flush_task:
        webhook_service._node_event_flush_task.cancel()
        try:
            await webhook_service._node_event_flush_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_coalesced_summary_truncates_and_reports_overflow(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """50 unique nodes + 7 overflow → 40 lines + 'truncated' line + 'отброшено' line."""
    max_lines = webhook_service._NODE_EVENT_SUMMARY_MAX_LINES
    payloads = [{'name': f'node-{i}', 'address': f'10.0.0.{i}'} for i in range(50)]

    sent_text: dict[str, str] = {}

    async def capture_send(text: str) -> bool:
        sent_text['value'] = text
        return True

    webhook_service._admin_service.send_webhook_notification = AsyncMock(side_effect=capture_send)

    await webhook_service._send_coalesced_node_notification('node.connection_lost', payloads, overflow_count=7)

    text = sent_text['value']
    bullet_lines = [line for line in text.split('\n') if line.startswith('•')]
    # 40 node lines + 1 "ещё N нод(ы) (truncated)" + 1 "событий отброшено (buffer overflow)"
    assert len(bullet_lines) == max_lines + 2
    assert '(truncated)' in text
    assert 'buffer overflow' in text
    # Header reports total = unique + overflow = 50 + 7 = 57
    assert '× 57' in text


@pytest.mark.asyncio
async def test_coalesced_summary_single_event_omits_count_suffix(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """One event → header without '× N' suffix."""
    sent_text: dict[str, str] = {}

    async def capture_send(text: str) -> bool:
        sent_text['value'] = text
        return True

    webhook_service._admin_service.send_webhook_notification = AsyncMock(side_effect=capture_send)

    await webhook_service._send_coalesced_node_notification(
        'node.connection_restored', [{'name': 'lone-node', 'address': '10.0.0.1'}]
    )

    assert '×' not in sent_text['value']
    assert 'lone-node' in sent_text['value']


@pytest.mark.asyncio
async def test_coalesced_summary_dedupes_by_name_and_address(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """Same (name, address) repeated 5 times → 1 line, header shows × 5 total."""
    payloads = [{'name': 'spammy', 'address': '10.0.0.1'} for _ in range(5)]

    sent_text: dict[str, str] = {}

    async def capture_send(text: str) -> bool:
        sent_text['value'] = text
        return True

    webhook_service._admin_service.send_webhook_notification = AsyncMock(side_effect=capture_send)

    await webhook_service._send_coalesced_node_notification('node.connection_lost', payloads)

    bullet_lines = [line for line in sent_text['value'].split('\n') if line.startswith('•')]
    assert len(bullet_lines) == 1
    assert 'spammy' in sent_text['value']


# ---------------------------------------------------------------------------
# Pending task tracking + graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_tracks_flush_task_in_pending_set(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """Active flush task is held in the strong-ref set; auto-removed after completion."""
    await webhook_service._enqueue_node_event('node.connection_lost', {'name': 'n1', 'address': '10.0.0.1'})

    task = webhook_service._node_event_flush_task
    assert task is not None
    assert task in webhook_service._node_event_pending_tasks

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    # Allow the done-callback to fire on the next event-loop iteration.
    await asyncio.sleep(0)

    assert task not in webhook_service._node_event_pending_tasks


@pytest.mark.asyncio
async def test_stop_drains_buffered_events(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """Pending events in the coalesce window get flushed on stop()."""
    sent_messages: list[str] = []

    async def capture_send(text: str) -> bool:
        sent_messages.append(text)
        return True

    webhook_service._admin_service.send_webhook_notification = AsyncMock(side_effect=capture_send)

    for i in range(3):
        await webhook_service._enqueue_node_event(
            'node.connection_lost', {'name': f'node-{i}', 'address': f'10.0.0.{i}'}
        )

    # Task is still sleeping in the coalesce window; stop() should cancel
    # it and send one summary anyway.
    await webhook_service.stop()

    assert len(sent_messages) == 1
    assert '× 3' in sent_messages[0]
    assert 'node-0' in sent_messages[0]
    assert webhook_service._node_event_buffer == {}
    assert webhook_service._node_event_flush_task is None


@pytest.mark.asyncio
async def test_stop_is_idempotent_when_buffer_empty(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """Calling stop() with no pending events sends nothing and doesn't crash."""
    webhook_service._admin_service.send_webhook_notification = AsyncMock(return_value=True)

    await webhook_service.stop()

    webhook_service._admin_service.send_webhook_notification.assert_not_called()
    assert webhook_service._node_event_flush_task is None


@pytest.mark.asyncio
async def test_enqueue_blocked_after_stop(
    webhook_service: RemnaWaveWebhookService,
) -> None:
    """After stop() the service refuses new enqueues — no orphaned flush tasks."""
    webhook_service._admin_service.send_webhook_notification = AsyncMock(return_value=True)
    await webhook_service.stop()
    assert webhook_service._stopped is True

    accepted = await webhook_service._enqueue_node_event(
        'node.connection_lost', {'name': 'late', 'address': '10.0.0.99'}
    )

    assert accepted is False
    assert webhook_service._node_event_buffer == {}
    assert webhook_service._node_event_flush_task is None


@pytest.mark.asyncio
async def test_send_message_logs_clamped_retry_after(
    admin_service: AdminNotificationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retry_after=120 → log includes both clamped value (30) and original (120)."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr('app.services.admin_notification_service.asyncio.sleep', fake_sleep)

    captured: list[dict] = []

    structlog_mod = __import__('app.services.admin_notification_service', fromlist=['logger'])
    original_warning = structlog_mod.logger.warning

    def capturing_warning(msg, **kwargs):
        captured.append({'msg': msg, **kwargs})
        return original_warning(msg, **kwargs)

    monkeypatch.setattr(structlog_mod.logger, 'warning', capturing_warning)

    flood = TelegramRetryAfter(method=SimpleNamespace(), message='flood', retry_after=120)
    admin_service.bot.send_message = AsyncMock(side_effect=[flood, None])

    await admin_service._send_message('hi', category=NotificationCategory.INFRASTRUCTURE)

    flood_logs = [c for c in captured if 'flood control' in c['msg']]
    assert len(flood_logs) >= 1
    log = flood_logs[0]
    assert log['retry_after'] == 30
    assert log.get('clamped') is True
    assert log.get('retry_after_requested') == 120


# ---------------------------------------------------------------------------
# Token redaction inside TelegramNotifierProcessor (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_notifier_processor_redacts_token_in_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a future aiogram leaks the bot token in exc str / traceback, it gets redacted."""
    from app.logging_handler import TelegramNotifierProcessor

    captured: dict[str, Any] = {}

    async def fake_send_error_to_admin_chat(_bot, error, context, *, tb_override=None):
        captured['error_str'] = str(error)
        captured['context'] = context
        captured['tb_override'] = tb_override

    import app.middlewares.global_error as ge

    monkeypatch.setattr(ge, 'send_error_to_admin_chat', fake_send_error_to_admin_chat)

    bot = MagicMock()
    leaked = (
        'failed to POST https://api.telegram.org/bot8123456789:AAH-Sample_TokenString_With-Chars-aiogram01/sendMessage'
    )
    event_dict = {
        'event': leaked,
        'logger': 'app.services.notification_delivery_service',
        'level': 'error',
        'exc_info': None,
    }

    await TelegramNotifierProcessor._send(bot, event_dict)

    assert '8123456789:AAH' not in captured.get('error_str', '')
    assert 'bot[REDACTED]' in captured.get('error_str', '')
    if captured.get('context'):
        assert '8123456789:AAH' not in captured['context']
    if captured.get('tb_override'):
        assert '8123456789:AAH' not in captured['tb_override']
