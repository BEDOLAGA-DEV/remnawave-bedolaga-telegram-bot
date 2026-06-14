from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin.messages import _deliver_broadcast_to, _send_test_broadcast


@pytest.fixture
def bot():
    return SimpleNamespace(
        copy_message=AsyncMock(),
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_video=AsyncMock(),
        send_document=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_copy_mode_calls_copy_message(bot):
    kb = object()
    await _deliver_broadcast_to(
        bot, 555, mode='copy', message_text='', media_type=None, media_file_id=None,
        copy_from_chat_id=111, copy_source_message_id=222, reply_markup=kb,
    )
    bot.copy_message.assert_awaited_once_with(
        chat_id=555, from_chat_id=111, message_id=222, reply_markup=kb,
    )
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_text_mode_calls_send_message(bot):
    kb = object()
    await _deliver_broadcast_to(
        bot, 555, mode='html', message_text='hi', media_type=None, media_file_id=None,
        copy_from_chat_id=None, copy_source_message_id=None, reply_markup=kb,
    )
    bot.send_message.assert_awaited_once_with(
        chat_id=555, text='hi', parse_mode='HTML', reply_markup=kb,
    )
    bot.copy_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_photo_mode_calls_send_photo(bot):
    kb = object()
    await _deliver_broadcast_to(
        bot, 555, mode='html', message_text='cap', media_type='photo', media_file_id='fid',
        copy_from_chat_id=None, copy_source_message_id=None, reply_markup=kb,
    )
    bot.send_photo.assert_awaited_once_with(
        chat_id=555, photo='fid', caption='cap', parse_mode='HTML', reply_markup=kb,
    )


@pytest.mark.asyncio
async def test_send_test_broadcast_copy_ok(bot):
    ok, reason = await _send_test_broadcast(
        bot, 999, mode='copy', message_text='', media_type=None, media_file_id=None,
        copy_from_chat_id=11, copy_source_message_id=22, reply_markup=None,
    )
    assert ok is True
    assert reason == ''
    bot.copy_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_test_broadcast_reports_blocked(bot):
    from aiogram.exceptions import TelegramForbiddenError

    bot.copy_message.side_effect = TelegramForbiddenError(method='copyMessage', message='blocked')
    ok, reason = await _send_test_broadcast(
        bot, 999, mode='copy', message_text='', media_type=None, media_file_id=None,
        copy_from_chat_id=11, copy_source_message_id=22, reply_markup=None,
    )
    assert ok is False
    assert 'не запускал' in reason or 'не найден' in reason
