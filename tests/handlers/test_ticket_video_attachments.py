"""Видео во вложении тикета не теряется (#видео в ответах поддержки).

Telegram-хендлеры тикетов распознавали только фото (`if message.photo:`):

  * видео без подписи отвергалось «Ответ должен содержать минимум 5 символов»;
  * видео с подписью проходило, но сохранялось и пересылалось одной подписью —
    сам файл молча терялся, и ни отправитель, ни получатель об этом не знали;
  * ответ пользователя с фото, но без подписи отвергался тем же сообщением.

Кабинет и webapi видео в тикетах принимают (`app/cabinet/schemas/tickets.py`),
так что тип 'video' в `ticket_messages.media_type` для бота не новый.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types

from app.services.admin_notification_service import AdminNotificationService
from app.utils.ticket_media import (
    build_ticket_media_group,
    extract_ticket_media,
    has_ticket_media,
    ticket_media_label,
)


APP_DIR = Path(__file__).resolve().parents[2] / 'app'

INPUT_HANDLERS = [
    (APP_DIR / 'handlers' / 'tickets.py', 'handle_ticket_message_input'),
    (APP_DIR / 'handlers' / 'tickets.py', 'handle_ticket_reply'),
    (APP_DIR / 'handlers' / 'admin' / 'tickets.py', 'handle_admin_ticket_reply'),
]


def _message(*, photo=None, video=None, caption=None, text=None):
    return SimpleNamespace(photo=photo, video=video, caption=caption, text=text)


def _ticket_message(media_type, file_id='file-id', has_media=True):
    return SimpleNamespace(has_media=has_media, media_type=media_type, media_file_id=file_id)


# ---------------------------------------------------------------------------
# extract_ticket_media
# ---------------------------------------------------------------------------


def test_extract_video_with_caption():
    message = _message(video=SimpleNamespace(file_id='vid'), caption='смотрите видео')
    assert extract_ticket_media(message) == ('video', 'vid', 'смотрите видео')


def test_extract_video_without_caption():
    message = _message(video=SimpleNamespace(file_id='vid'))
    assert extract_ticket_media(message) == ('video', 'vid', None)


def test_extract_photo_takes_largest_size():
    message = _message(photo=[SimpleNamespace(file_id='small'), SimpleNamespace(file_id='large')], caption='фото')
    assert extract_ticket_media(message) == ('photo', 'large', 'фото')


def test_extract_plain_text_has_no_media():
    assert extract_ticket_media(_message(text='просто текст')) == (None, None, None)


# ---------------------------------------------------------------------------
# Карточка тикета и просмотр вложений
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('media_type', 'label'),
    [('photo', '📎 Вложение: фото\n'), ('video', '📎 Вложение: видео\n'), ('document', ''), (None, '')],
)
def test_ticket_media_label(media_type, label):
    assert ticket_media_label(media_type) == label


def test_has_ticket_media_requires_known_type_and_flag():
    assert has_ticket_media(_ticket_message('photo'))
    assert has_ticket_media(_ticket_message('video'))
    assert not has_ticket_media(_ticket_message('document'))
    assert not has_ticket_media(_ticket_message('video', has_media=False))


def test_media_group_mixes_photos_and_videos_in_order():
    group = build_ticket_media_group(
        [
            _ticket_message('photo', 'p1'),
            _ticket_message('video', 'v1'),
            _ticket_message('document', 'd1'),
            _ticket_message('video', None),
            _ticket_message('photo', 'p2'),
        ]
    )
    assert [(type(item), item.media) for item in group] == [
        (types.InputMediaPhoto, 'p1'),
        (types.InputMediaVideo, 'v1'),
        (types.InputMediaPhoto, 'p2'),
    ]


# ---------------------------------------------------------------------------
# Хендлеры: тип вложения определяется только через extract_ticket_media
# ---------------------------------------------------------------------------


def _find_function(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f'async function {name!r} not found in {path.name}')


def test_input_handlers_use_shared_media_extraction():
    for path, func_name in INPUT_HANDLERS:
        func = _find_function(path, func_name)
        calls = {
            node.func.id for node in ast.walk(func) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert 'extract_ticket_media' in calls, (
            f'{path.name}:{func_name} должен брать вложение через extract_ticket_media'
        )

        direct_photo = [
            node.lineno
            for node in ast.walk(func)
            if isinstance(node, ast.Attribute)
            and node.attr == 'photo'
            and isinstance(node.value, ast.Name)
            and node.value.id == 'message'
        ]
        assert not direct_photo, f'{path.name}:{func_name} проверяет message.photo напрямую (строки {direct_photo})'


def test_user_reply_length_check_allows_media_without_caption():
    func = _find_function(APP_DIR / 'handlers' / 'tickets.py', 'handle_ticket_reply')
    length_checks = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.If)
        and any(
            isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == 'len'
            for sub in ast.walk(node.test)
        )
        and any(isinstance(sub, ast.Compare) and isinstance(sub.ops[0], ast.Lt) for sub in ast.walk(node.test))
    ]
    assert length_checks, 'проверка минимальной длины ответа не найдена'
    for node in length_checks:
        names = {sub.id for sub in ast.walk(node.test) if isinstance(sub, ast.Name)}
        assert 'media_file_id' in names, (
            f'tickets.py:{node.lineno} требует минимум текста даже при вложении — фото или видео без подписи отвергается'
        )


# ---------------------------------------------------------------------------
# Уведомление в тикет-топик админов
# ---------------------------------------------------------------------------


def _service():
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_video = AsyncMock()
    service = AdminNotificationService(bot)
    service.chat_id = -100123
    service.ticket_topic_id = None
    service.topic_id = 7
    service._send_message = AsyncMock(return_value=True)
    return service, bot


async def test_admin_topic_notification_sends_video():
    service, bot = _service()
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[])

    assert await service._send_ticket_media_notification('<b>ответ</b>', 'video', 'vid', keyboard)

    bot.send_video.assert_awaited_once()
    bot.send_photo.assert_not_awaited()
    kwargs = bot.send_video.await_args.kwargs
    assert kwargs['video'] == 'vid'
    assert kwargs['caption'] == '<b>ответ</b>'
    assert kwargs['message_thread_id'] == 7
    assert kwargs['reply_markup'] is keyboard


async def test_admin_topic_notification_long_text_goes_before_video():
    service, bot = _service()

    assert await service._send_ticket_media_notification('я' * 2000, 'video', 'vid')

    service._send_message.assert_awaited_once()
    kwargs = bot.send_video.await_args.kwargs
    assert kwargs['video'] == 'vid'
    assert 'caption' not in kwargs


async def test_admin_topic_notification_still_sends_photo():
    service, bot = _service()

    assert await service._send_ticket_media_notification('ответ', 'photo', 'pic')

    bot.send_photo.assert_awaited_once()
    bot.send_video.assert_not_awaited()
    assert bot.send_photo.await_args.kwargs['photo'] == 'pic'
