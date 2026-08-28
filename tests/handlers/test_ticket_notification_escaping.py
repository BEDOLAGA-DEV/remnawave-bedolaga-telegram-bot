"""Превью ответа в уведомлении экранируется — иначе оно не доходит вовсе.

Бот создаётся с ``DefaultBotProperties(parse_mode=HTML)``, а превью ответа
поддержки подставляется в текст уведомления как есть. Ответ вида «откройте
<config> и замените &key» Telegram разбирает как разметку и отвечает
``can't parse entities``; исключение проглатывается общим ``except``, и
пользователь просто не узнаёт, что ему ответили.

Раньше окно превью было 100 символов, теперь 500 — попасть в него угловой
скобкой стало впятеро проще.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.admin.tickets import notify_user_about_ticket_reply


DANGEROUS_REPLY = 'Откройте файл <config.json> и замените ключ &key на свой'


def _ticket() -> SimpleNamespace:
    return SimpleNamespace(
        id=17,
        user=SimpleNamespace(telegram_id=148871030, language='ru', username='client'),
    )


async def _send_notification(reply_text: str) -> str:
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    with (
        patch(
            'app.handlers.admin.tickets.SupportSettingsService.get_user_ticket_notifications_enabled',
            staticmethod(lambda: True),
        ),
        patch('app.handlers.admin.tickets.TicketMessageCRUD.get_last_message', AsyncMock(return_value=None)),
    ):
        await notify_user_about_ticket_reply(bot, _ticket(), reply_text, AsyncMock())

    bot.send_message.assert_awaited_once()
    return bot.send_message.await_args.kwargs['text']


@pytest.mark.asyncio
async def test_reply_preview_is_escaped() -> None:
    text = await _send_notification(DANGEROUS_REPLY)

    assert '&lt;config.json&gt;' in text
    assert '&amp;key' in text
    # Ни одной сырой скобки из ответа — иначе Telegram примет её за тег
    assert '<config.json>' not in text


@pytest.mark.asyncio
async def test_escaping_does_not_split_entities_on_the_cut() -> None:
    """Обрезка идёт до экранирования, поэтому `&quot;` не разрывается.

    При обратном порядке хвост страницы обрывался бы на `&qu`, и Telegram
    отклонил бы уведомление ровно так же, как при сыром `<`.
    """
    text = await _send_notification('"' * 900)

    assert '&qu' not in text.replace('&quot;', '')
    assert text.count('&quot;') == 500


@pytest.mark.asyncio
async def test_plain_reply_is_unchanged() -> None:
    text = await _send_notification('Проверьте настройки и напишите, если не поможет')

    assert 'Проверьте настройки и напишите, если не поможет' in text
