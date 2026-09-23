"""Сообщение об остановке бота: причина, сколько проработал, что делать дальше."""

from __future__ import annotations

import ast
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.startup_notification_service import (
    ShutdownReason,
    format_uptime,
    render_shutdown_message,
    send_shutdown_notification,
)
from tests.services.test_startup_notification_summary import _assert_valid_telegram_html


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
STARTED = NOW - timedelta(days=3, hours=4, minutes=12)
MAIN = Path('main.py')


def _render(reason: ShutdownReason, started_at: datetime | None = STARTED) -> str:
    return render_shutdown_message(reason, version='4.14.0', started_at=started_at, now=NOW)


def test_docker_stop_is_a_planned_shutdown_with_a_hint():
    text = _render(ShutdownReason(signum=signal.SIGTERM.value))

    _assert_valid_telegram_html(text)
    assert text.startswith('🛑')
    assert 'плановая остановка, сигнал SIGTERM' in text
    assert 'docker compose stop' in text
    assert 'Проработал: <b>3 д 4 ч 12 мин</b>' in text
    assert 'дождитесь сообщения о запуске' in text


def test_ctrl_c_is_named():
    assert 'SIGINT (Ctrl+C)' in _render(ShutdownReason(signum=signal.SIGINT.value))


def test_unknown_signal_is_named_by_its_name():
    assert 'сигнал SIGHUP' in _render(ShutdownReason(signum=signal.SIGHUP.value))


def test_polling_crash_is_a_failure_with_the_error_and_advice():
    error = RuntimeError('Unauthorized: bot token is invalid <x>')

    text = _render(ShutdownReason(error=error, source='polling'))

    _assert_valid_telegram_html(text)
    assert text.startswith('🔴')
    assert 'ошибка — Telegram polling' in text
    assert 'RuntimeError: Unauthorized: bot token is invalid &lt;x&gt;' in text
    assert 'Рекомендации' in text, 'для известной ошибки — подсказка, как чинить'
    assert 'поднимет бота заново' in text
    assert 'Если это перезапуск' not in text


def test_long_error_is_cut():
    text = _render(ShutdownReason(error=ValueError('x' * 5000), source='main_loop'))

    assert 'основной цикл' in text
    assert 'x' * 400 not in text


def test_without_start_time_there_is_no_uptime():
    text = _render(ShutdownReason(signum=signal.SIGTERM.value), started_at=None)

    assert 'Проработал' not in text
    assert 'Остановлен' in text


@pytest.mark.parametrize(
    ('delta', 'expected'),
    [
        (timedelta(seconds=20), 'меньше минуты'),
        (timedelta(minutes=5), '5 мин'),
        (timedelta(hours=2), '2 ч'),
        (timedelta(days=1, minutes=3), '1 д 3 мин'),
        (timedelta(days=3, hours=4, minutes=12), '3 д 4 ч 12 мин'),
    ],
)
def test_uptime_format(delta, expected):
    assert format_uptime(delta) == expected


@pytest.fixture
def admin_chat(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', -100500)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', 1)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID', 2)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', 3)


@pytest.mark.asyncio
async def test_planned_stop_goes_to_the_infrastructure_topic(admin_chat):
    bot = AsyncMock()

    assert await send_shutdown_notification(bot, ShutdownReason(signum=15), started_at=STARTED)

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['message_thread_id'] == 2
    assert 'reply_markup' not in kwargs


@pytest.mark.asyncio
async def test_crash_goes_to_the_errors_topic_with_a_contact_button(admin_chat):
    bot = AsyncMock()

    await send_shutdown_notification(
        bot, ShutdownReason(error=RuntimeError('boom'), source='polling'), started_at=STARTED
    )

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['message_thread_id'] == 3
    assert kwargs['reply_markup'].inline_keyboard[0][0].text == '💬 Сообщить разработчику'


@pytest.mark.asyncio
async def test_disabled_notifications_send_nothing(admin_chat, monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    bot = AsyncMock()

    assert not await send_shutdown_notification(bot, ShutdownReason(signum=15), started_at=STARTED)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_does_not_raise(admin_chat):
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError('network down')

    assert not await send_shutdown_notification(bot, ShutdownReason(signum=15), started_at=STARTED)


def _main_function() -> ast.AsyncFunctionDef:
    tree = ast.parse(MAIN.read_text(encoding='utf-8'))
    return next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'main')


def test_main_does_not_shadow_datetime_locally():
    """Локальный ``from datetime import datetime`` в ветке делает имя локальным для всей
    ``main()``: ``datetime.now(UTC)`` при старте падал бы UnboundLocalError."""
    shadowing = [
        node.lineno
        for node in ast.walk(_main_function())
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any((alias.asname or alias.name) == 'datetime' for alias in node.names)
    ]
    assert shadowing == []


def test_shutdown_notice_is_sent_before_services_are_stopped():
    """Docker даёт на остановку ~10 секунд: сообщение — первым делом, пока сессия жива."""
    source = MAIN.read_text(encoding='utf-8')
    finally_start = source.index("logger.info('🛑 Начинается корректное завершение работы...')")

    notice = source.index('send_shutdown_notification(', finally_start)
    first_stop = source.index('auto_payment_verification_service.stop()', finally_start)
    session_close = source.index('await bot.session.close()', finally_start)

    assert notice < first_stop < session_close
