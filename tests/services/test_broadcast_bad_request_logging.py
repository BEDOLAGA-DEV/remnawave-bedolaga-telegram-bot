"""Отказ Telegram (BadRequest) в рассылке не должен исчезать молча.

Жалоба: рассылка с картинкой падает у всех 25 получателей за секунду,
failed = total, а в логах и в журнале системных ошибок — пусто. Причина: ветка
``except TelegramBadRequest`` возвращала 'failed', не записав текст ошибки.
Администратор видел счётчик и не мог понять, что именно отверг Telegram
(слишком длинная подпись, битая HTML-разметка, неверный file_id).

Ожидание: первая ошибка по каждой причине уходит в error (значит — админу и в
журнал системных ошибок) с текстом от Telegram, повторы не спамят, а в конце
рассылки есть сводка «сколько отказов по какой причине».
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from aiogram.exceptions import TelegramBadRequest

from app.services.broadcast_service import BroadcastConfig, BroadcastMediaConfig, BroadcastService


CAPTION_TOO_LONG = TelegramBadRequest(method=None, message='Bad Request: MEDIA_CAPTION_TOO_LONG')
CANT_PARSE = TelegramBadRequest(method=None, message="Bad Request: can't parse entities")
CHAT_NOT_FOUND = TelegramBadRequest(method=None, message='Bad Request: chat not found')


def _media_config(recipients: list[int]) -> BroadcastConfig:
    return BroadcastConfig(
        target='all',
        message_text='Промо',
        selected_buttons=[],
        media=BroadcastMediaConfig(type='photo', file_id='A' * 32, caption='x' * 1100),
        recipient_ids=recipients,
    )


async def _noop(*_args, **_kwargs) -> None:
    return None


async def _run(service: BroadcastService, recipients: list[int], monkeypatch) -> tuple[int, int, int, bool]:
    monkeypatch.setattr(service, '_update_progress', _noop)
    monkeypatch.setattr('app.services.broadcast_service._TG_BATCH_DELAY', 0)
    return await service._send_batched(7, recipients, _media_config(recipients), None, asyncio.Event())


async def test_bad_request_is_logged_once_per_cause_with_telegram_text(monkeypatch):
    service = BroadcastService()

    async def reject(*_args):
        raise CAPTION_TOO_LONG

    monkeypatch.setattr(service, '_deliver_message', reject)
    with patch('app.services.broadcast_service.logger') as logger:
        sent, failed, blocked, cancelled = await _run(service, [1, 2, 3], monkeypatch)

    assert (sent, failed, blocked, cancelled) == (0, 3, 0, False)

    assert logger.error.call_count == 1
    first = logger.error.call_args.kwargs
    assert 'MEDIA_CAPTION_TOO_LONG' in first['error']
    assert first['broadcast_id'] == 7
    assert first['media_type'] == 'photo'
    assert first['caption_length'] == 1100

    summary = logger.warning.call_args.kwargs
    assert summary['broadcast_id'] == 7
    assert sum(summary['failed_by_error'].values()) == 3
    assert any('MEDIA_CAPTION_TOO_LONG' in cause for cause in summary['failed_by_error'])


async def test_each_distinct_cause_gets_its_own_error_entry(monkeypatch):
    service = BroadcastService()

    async def reject(telegram_id, *_args):
        raise CAPTION_TOO_LONG if telegram_id % 2 else CANT_PARSE

    monkeypatch.setattr(service, '_deliver_message', reject)
    with patch('app.services.broadcast_service.logger') as logger:
        _sent, failed, _blocked, _cancelled = await _run(service, [1, 2, 3, 4], monkeypatch)

    assert failed == 4
    logged = [call.kwargs['error'] for call in logger.error.call_args_list]
    assert len(logged) == 2
    assert any('MEDIA_CAPTION_TOO_LONG' in text for text in logged)
    assert any("can't parse entities" in text for text in logged)


async def test_blocked_users_are_still_counted_quietly(monkeypatch):
    """«chat not found» — это не сбой рассылки, а ушедший пользователь: без error."""
    service = BroadcastService()

    async def reject(*_args):
        raise CHAT_NOT_FOUND

    monkeypatch.setattr(service, '_deliver_message', reject)
    with patch('app.services.broadcast_service.logger') as logger:
        sent, failed, blocked, _cancelled = await _run(service, [1, 2], monkeypatch)

    assert (sent, failed, blocked) == (0, 0, 2)
    logger.error.assert_not_called()
    logger.warning.assert_not_called()
