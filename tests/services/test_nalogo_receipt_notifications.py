"""Доставка чеков NaloGO (#3082): клиенту в Telegram + дубль в админ-топик.

Раньше чек создавался и сохранялся в транзакцию, но никуда не отправлялся —
покупатель его не видел (по 422-ФЗ самозанятый обязан передать чек), а админ
узнавал о чеках только из ЛК налоговой.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import app.services.nalogo_service as _nalogo_module
from app.config import settings
from app.services.nalogo_service import send_nalogo_receipt_notifications


# Autouse-фикстура ниже подменяет _download_receipt_file, поэтому настоящую
# функцию забираем сейчас — иначе тесты самого скачивания проверяли бы мок.
_REAL_DOWNLOAD = _nalogo_module._download_receipt_file


@pytest.fixture(autouse=True)
def _no_receipt_download(monkeypatch):
    """По умолчанию скачивание чека недоступно — тесты проверяют фолбэк-путь
    (текст со ссылкой), не выходя в сеть. Тесты файловой доставки переопределяют
    мок точечно."""
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(return_value=None),
    )


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


def _nalogo(url: str | None = 'https://lknpd.nalog.ru/api/v1/receipt/123456789/uuid-1/print') -> SimpleNamespace:
    return SimpleNamespace(get_receipt_print_url=lambda receipt_uuid: url)


class _FakeSessionCtx:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, *args):
        return False


def _patch_user_lookup(monkeypatch, db_user):
    monkeypatch.setattr('app.database.database.AsyncSessionLocal', lambda: _FakeSessionCtx())
    monkeypatch.setattr('app.database.crud.user.get_user_by_telegram_id', AsyncMock(return_value=db_user))


async def test_sends_to_user_and_duplicates_to_admin_topic(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500', raising=False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', 77, raising=False)
    _patch_user_lookup(
        monkeypatch,
        SimpleNamespace(first_name='Вася', last_name='<Пупкин>', username='vasya', email='v@example.com'),
    )
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
        context_label='Источник: YooKassa',
    )

    assert bot.send_message.await_count == 2
    user_call, admin_call = bot.send_message.await_args_list
    assert user_call.kwargs['chat_id'] == 111
    assert 'Чек по вашему платежу' in user_call.kwargs['text']
    assert user_call.kwargs['reply_markup'].inline_keyboard[0][0].url.endswith('/print')

    assert admin_call.kwargs['chat_id'] == -100500
    assert admin_call.kwargs['message_thread_id'] == 77
    admin_text = admin_call.kwargs['text']
    assert 'Источник: YooKassa' in admin_text
    assert '&lt;Пупкин&gt;' in admin_text  # имя экранировано — сырой HTML не ломает разметку
    assert '<Пупкин>' not in admin_text
    assert 'v@example.com' in admin_text


async def test_no_telegram_id_admin_only_with_guest_mark(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500', raising=False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', None, raising=False)
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=None,
    )

    assert bot.send_message.await_count == 1
    admin_call = bot.send_message.await_args_list[0]
    assert admin_call.kwargs['chat_id'] == -100500
    assert 'без Telegram' in admin_call.kwargs['text']


async def test_user_send_failure_does_not_block_admin_duplicate(monkeypatch):
    """Юзер заблокировал бота — админ-топик всё равно получает чек."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500', raising=False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', None, raising=False)
    _patch_user_lookup(monkeypatch, None)
    bot = _bot()
    forbidden = TelegramForbiddenError(method=MagicMock(), message='blocked')
    bot.send_message = AsyncMock(side_effect=[forbidden, None])

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    assert bot.send_message.await_count == 2  # упавший юзер-send + успешный админ-send


async def test_no_print_url_sends_nothing():
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(url=None),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    bot.send_message.assert_not_awaited()


async def test_no_admin_chat_user_only(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    _patch_user_lookup(monkeypatch, None)
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args_list[0].kwargs['chat_id'] == 111


def test_get_receipt_print_url_builds_v1_link():
    """Ссылка обязана содержать /v1 — библиотечный print_url() строит без него
    (нерабочая), поэтому URL собирается вручную."""
    from app.services.nalogo_service import NaloGoService

    service = NaloGoService.__new__(NaloGoService)
    service.configured = True
    service.client = SimpleNamespace(base_url='https://lknpd.nalog.ru/api/')
    service.inn = '123456789012'

    url = service.get_receipt_print_url(' uuid-42 ')
    assert url == 'https://lknpd.nalog.ru/api/v1/receipt/123456789012/uuid-42/print'

    service.configured = False
    assert service.get_receipt_print_url('uuid-42') is None


async def test_receipt_delivered_as_photo_when_download_succeeds(monkeypatch):
    """lknpd недоступен клиентам за VPN — при успешном серверном скачивании чек
    уходит фотографией (юзеру и в админ-топик), ссылка остаётся кнопкой."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500', raising=False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', 77, raising=False)
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(return_value=(b'jpeg-bytes', 'image/jpeg')),
    )
    _patch_user_lookup(monkeypatch, SimpleNamespace(first_name='Вася', last_name=None, username=None, email=None))
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    bot.send_message.assert_not_awaited()
    assert bot.send_photo.await_count == 2
    user_call, admin_call = bot.send_photo.await_args_list
    assert user_call.kwargs['chat_id'] == 111
    assert 'Чек по вашему платежу' in user_call.kwargs['caption']
    assert user_call.kwargs['photo'].filename == 'receipt_uuid-1.jpg'
    assert user_call.kwargs['reply_markup'].inline_keyboard[0][0].url.endswith('/print')
    assert admin_call.kwargs['chat_id'] == -100500
    assert admin_call.kwargs['message_thread_id'] == 77


async def test_receipt_delivered_as_document_for_pdf(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(return_value=(b'%PDF-1.4', 'application/pdf')),
    )
    _patch_user_lookup(monkeypatch, None)
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    assert bot.send_document.await_count == 1
    assert bot.send_document.await_args_list[0].kwargs['document'].filename == 'receipt_uuid-1.pdf'


async def test_download_failure_falls_back_to_link(monkeypatch):
    """Сбой скачивания (сеть/503 ФНС) не ломает доставку — уходит текст со ссылкой."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(side_effect=RuntimeError('boom')),
    )
    _patch_user_lookup(monkeypatch, None)
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    bot.send_photo.assert_not_awaited()
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args_list[0].kwargs['chat_id'] == 111


async def test_telegram_rejects_file_falls_back_to_link(monkeypatch):
    """Telegram отверг сам файл — чек обязан дойти, поэтому уходит ссылкой.

    Без фолбэка покупатель не получал бы чек вообще: ошибка гасилась
    внешним except и доставка молча терялась (нарушение 422-ФЗ).
    """
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(return_value=(b'jpeg-bytes', 'image/jpeg')),
    )
    _patch_user_lookup(monkeypatch, None)
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message='PHOTO_INVALID_DIMENSIONS'))

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_message.await_count == 1
    user_call = bot.send_message.await_args_list[0]
    assert user_call.kwargs['chat_id'] == 111
    assert user_call.kwargs['reply_markup'].inline_keyboard[0][0].url.endswith('/print')


async def test_blocked_user_is_not_retried_as_message(monkeypatch):
    """Юзер заблокировал бота — это не проблема файла, повторять текстом бессмысленно."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(return_value=(b'jpeg-bytes', 'image/jpeg')),
    )
    _patch_user_lookup(monkeypatch, None)
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message='blocked'))

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()


class _FakeResponse:
    def __init__(self, *, status=200, content_type='image/jpeg', body=b'jpeg-bytes', content_length=None):
        self.status = status
        self.headers = {'Content-Type': content_type}
        self.content_length = content_length if content_length is not None else len(body)
        self.content = SimpleNamespace(read=AsyncMock(return_value=body))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def get(self, *args, **kwargs):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _patch_aiohttp(monkeypatch, response):
    import aiohttp

    monkeypatch.setattr(aiohttp, 'ClientSession', lambda *a, **kw: _FakeSession(response))
    monkeypatch.setattr(settings, 'NALOGO_PROXY_URL', None, raising=False)


async def test_download_rejects_html_error_page(monkeypatch):
    """ФНС отдаёт HTML-заглушку с кодом 200 — её нельзя слать как «чек»."""
    _patch_aiohttp(monkeypatch, _FakeResponse(content_type='text/html; charset=utf-8', body=b'<html>error</html>'))

    assert await _REAL_DOWNLOAD('https://lknpd.nalog.ru/api/v1/receipt/1/u/print') is None


async def test_download_accepts_image_and_pdf(monkeypatch):
    _patch_aiohttp(monkeypatch, _FakeResponse(content_type='image/jpeg', body=b'jpeg'))
    assert await _REAL_DOWNLOAD('https://x/print') == (b'jpeg', 'image/jpeg')

    _patch_aiohttp(monkeypatch, _FakeResponse(content_type='application/pdf', body=b'%PDF'))
    assert await _REAL_DOWNLOAD('https://x/print') == (b'%PDF', 'application/pdf')


async def test_download_rejects_oversized_receipt(monkeypatch):
    """Предохранитель от вычитывания мусора в память."""
    from app.services.nalogo_service import _RECEIPT_MAX_BYTES

    # Заявленный Content-Length больше лимита — не читаем вовсе
    _patch_aiohttp(monkeypatch, _FakeResponse(content_length=_RECEIPT_MAX_BYTES + 1))
    assert await _REAL_DOWNLOAD('https://x/print') is None

    # Content-Length занижен/отсутствует, но тело превышает лимит при чтении
    _patch_aiohttp(monkeypatch, _FakeResponse(body=b'x' * (_RECEIPT_MAX_BYTES + 1), content_length=0))
    assert await _REAL_DOWNLOAD('https://x/print') is None
