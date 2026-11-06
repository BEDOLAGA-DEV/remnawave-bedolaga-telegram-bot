"""Staging uploads must use the configured notification topic, not closed General."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.cabinet.routes import media
from app.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize('media_type', ['photo', 'video', 'document'])
@pytest.mark.parametrize('destination', ['notification_topic', 'admin_private', 'no_topic'])
async def test_upload_staging_respects_notification_destination(monkeypatch, media_type, destination):
    notification_chat = None if destination == 'admin_private' else -1001234567890
    configured_topic = None if destination == 'no_topic' else 42
    expected_chat = 1 if destination == 'admin_private' else notification_chat
    expected_topic = 42 if destination == 'notification_topic' else None
    monkeypatch.setattr(type(settings), 'get_admin_notifications_chat_id', lambda _: notification_chat)
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda _: [1])
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', configured_topic)
    # The staging endpoint also serves broadcasts; it follows the general notification destination.
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID', 99)
    stored_media = SimpleNamespace(file_id='test_file_id', file_unique_id='test_unique_id')
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        assert kwargs['chat_id'] == expected_chat
        if destination == 'notification_topic' and kwargs.get('message_thread_id') != 42:
            raise TelegramBadRequest(method=SendMessage(chat_id=expected_chat, text='test'), message='TOPIC_CLOSED')
        assert kwargs.get('message_thread_id') == expected_topic
        return SimpleNamespace(message_id=10, photo=[stored_media], video=stored_media, document=stored_media)

    bot = SimpleNamespace(
        send_photo=send,
        send_video=send,
        send_document=send,
        delete_message=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(media, 'create_bot', lambda: bot)
    monkeypatch.setattr(media, '_build_media_url', lambda request, file_id: f'/media/{file_id}')
    uploaded = UploadFile(
        filename='test.png', file=BytesIO(b'test upload'), headers=Headers({'content-type': 'image/png'})
    )

    result = await media.upload_media(
        request=SimpleNamespace(), user=SimpleNamespace(telegram_id=1), file=uploaded, media_type=media_type
    )

    assert result.file_id == 'test_file_id'
    assert result.media_type == media_type
    assert len(calls) == 1
    bot.delete_message.assert_awaited_once_with(chat_id=expected_chat, message_id=10)
    bot.session.close.assert_awaited_once()
