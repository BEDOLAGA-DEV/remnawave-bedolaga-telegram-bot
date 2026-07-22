"""Rich-отчёт об ошибках: трейсбеки инлайн в сворачиваемых блоках (#rich-admin)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import app.middlewares.global_error as ge
from app.config import settings
from app.utils import rich_admin


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    rich_admin._reset_rich_admin_availability()
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100123', raising=False)
    ge._error_buffer.clear()
    ge._last_error_notification = None
    yield
    ge._error_buffer.clear()
    ge._last_error_notification = None
    rich_admin._reset_rich_admin_availability()


def test_rich_error_report_structure():
    ge._error_buffer.extend(
        [
            ('ValueError', 'старое <сообщение>', 'Traceback (most recent call last):\n  old'),
            ('KeyError', 'свежее', 'Traceback (most recent call last):\n  fresh <tag>'),
        ]
    )

    report = ge._build_rich_error_report(datetime.now(UTC), 'KeyError', 'Logger: app.x')

    assert report.startswith('<h6>⚠️ Ошибка во время работы</h6>')
    assert '<code>KeyError</code>' in report
    assert 'Ошибок в отчёте:</b> 2' in report
    # Свежая ошибка развёрнута, старая свёрнута; содержимое экранировано
    assert '<details open><summary>📋 KeyError: свежее</summary>' in report
    assert '<details><summary>📋 ValueError: старое &lt;сообщение&gt;' in report
    assert 'fresh &lt;tag&gt;' in report
    assert '<footer>' in report


def test_rich_error_report_none_when_oversized():
    ge._error_buffer.append(('ValueError', 'msg', 'x' * (rich_admin.RICH_TEXT_LIMIT + 100)))
    assert ge._build_rich_error_report(datetime.now(UTC), 'ValueError', '') is None


async def test_send_error_uses_rich_and_clears_buffer():
    bot = AsyncMock()

    sent = await ge.send_error_to_admin_chat(bot, ValueError('boom'), context='Logger: app.x')

    assert sent is True
    bot.send_rich_message.assert_awaited_once()
    bot.send_document.assert_not_awaited()  # файл не нужен — всё инлайн
    assert ge._error_buffer == []
    html = bot.send_rich_message.await_args.kwargs['rich_message'].html
    assert '<pre><code class="language-python">' in html
    assert bot.send_rich_message.await_args.kwargs['reply_markup'] is not None


async def test_send_error_falls_back_to_document_when_rich_unavailable(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_RICH_ENABLED', False, raising=False)
    bot = AsyncMock()

    sent = await ge.send_error_to_admin_chat(bot, ValueError('boom'))

    assert sent is True
    bot.send_rich_message.assert_not_awaited()
    bot.send_document.assert_awaited_once()  # классический путь с .txt-файлом
