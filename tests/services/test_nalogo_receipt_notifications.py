"""Доставка чеков NaloGO (#3082): клиенту в Telegram + дубль в админ-топик.

Раньше чек создавался и сохранялся в транзакцию, но никуда не отправлялся —
покупатель его не видел (по 422-ФЗ самозанятый обязан передать чек), а админ
узнавал о чеках только из ЛК налоговой.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)

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


class _FakeStreamContent:
    """Эмулирует aiohttp StreamReader посегментно (портировано из #3096).

    Ключевое: read(n) с положительным n отдаёт лишь то, что «уже накопилось в
    буфере» — не больше одной сетевой порции за вызов, а не всё тело до n байт.
    Именно на этой семантике ловится регресс: код, вызывающий read(n) в расчёте
    «прочитает всё до лимита», получает обрезанный файл. Фейк, отдающий тело
    целиком одним вызовом, баг замаскировал бы (и маскировал до #3094/#3096).
    """

    def __init__(self, body: bytes, network_chunk_size: int = 8192):
        self._body = body
        self._network_chunk_size = network_chunk_size
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            # как настоящий StreamReader с n < 0 — дочитываем поток до конца
            chunk = self._body[self._pos :]
        else:
            chunk = self._body[self._pos : self._pos + min(n, self._network_chunk_size)]
        self._pos += len(chunk)
        return chunk

    async def iter_chunked(self, requested_size: int):
        # настоящий iter_chunked(n) — это ровно AsyncStreamIterator(read(n))
        while chunk := await self.read(requested_size):
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        status=200,
        content_type='image/jpeg',
        body=b'jpeg-bytes',
        content_length=None,
        network_chunk_size=8192,
    ):
        self.status = status
        self.headers = {'Content-Type': content_type}
        self.content_length = content_length if content_length is not None else len(body)
        self.content = _FakeStreamContent(body, network_chunk_size=network_chunk_size)

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


async def test_download_reads_full_body_not_just_first_network_chunk(monkeypatch):
    """Тело длиннее одной сетевой порции обязано склеиться целиком.

    Регрессия (#3094, #3096): resp.content.read(n) отдаёт только накопившийся
    буфер — первую сетевую порцию, а не всё тело до n байт. Чек уезжал клиенту
    физически обрезанным: валидный JPEG-заголовок, пустой/серый низ картинки.
    Тело здесь заведомо больше network_chunk_size и неоднородно, поэтому
    сравнение целиком ловит и обрыв, и перестановку, и потерю куска.
    """
    body = b'\xff\xd8' + bytes(range(256)) * 200 + b'\xff\xd9'  # ~51 КБ, 7 сетевых порций
    _patch_aiohttp(monkeypatch, _FakeResponse(content_type='image/jpeg', body=body, network_chunk_size=8192))

    result = await _REAL_DOWNLOAD('https://x/print')

    assert result is not None
    data, content_type = result
    assert content_type == 'image/jpeg'
    assert data == body, f'ожидали {len(body)} байт, получили {len(data)} — печатная форма чека обрезана'
    assert data.endswith(b'\xff\xd9'), 'файл должен заканчиваться JPEG-маркером EOI, а не обрывом на первом чанке'


async def test_download_rejects_oversized_receipt(monkeypatch):
    """Предохранитель от вычитывания мусора в память."""
    from app.services.nalogo_service import _RECEIPT_MAX_BYTES

    # Заявленный Content-Length больше лимита — не читаем вовсе
    _patch_aiohttp(monkeypatch, _FakeResponse(content_length=_RECEIPT_MAX_BYTES + 1))
    assert await _REAL_DOWNLOAD('https://x/print') is None

    # Content-Length занижен/отсутствует, но тело превышает лимит при чтении
    _patch_aiohttp(monkeypatch, _FakeResponse(body=b'x' * (_RECEIPT_MAX_BYTES + 1), content_length=0))
    assert await _REAL_DOWNLOAD('https://x/print') is None


# --- Email-фоллбек: чек уходит на почту, когда Telegram-доставка невозможна ---


def _patch_email(monkeypatch, configured=True):
    # Пакет app.cabinet.services реэкспортирует синглтон email_service, который
    # шадовит одноимённый сабмодуль при обычном import — берём модуль через
    # importlib, чтобы патчить именно тот инстанс, что использует продовый код.
    import importlib

    _email_module = importlib.import_module('app.cabinet.services.email_service')

    send_mock = MagicMock(return_value=True)
    monkeypatch.setattr(_email_module.email_service, 'send_email', send_mock)
    monkeypatch.setattr(_email_module.email_service, 'is_configured', lambda: configured)
    return send_mock


async def test_email_only_user_gets_receipt_by_email(monkeypatch):
    """У покупателя нет Telegram (кабинет/лендинг) — чек уходит на почту файлом."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(return_value=(b'jpeg-bytes', 'image/jpeg; charset=binary')),
    )
    send_mock = _patch_email(monkeypatch)
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=None,
        user_email='buyer@example.com',
    )

    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    send_mock.assert_called_once()
    to_email, subject, body_html, body_text, attachments = send_mock.call_args.args
    assert to_email == 'buyer@example.com'
    assert 'Чек' in subject
    assert '/print' in body_html
    # charset-суффикс Content-Type отрезан, вложение — исходные байты чека
    assert attachments == [('receipt_uuid-1.jpg', b'jpeg-bytes', 'image/jpeg')]


async def test_blocked_bot_falls_back_to_email_from_db(monkeypatch):
    """Юзер заблокировал бота — почту берём из БД, чек уходит письмом."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    _patch_user_lookup(
        monkeypatch,
        SimpleNamespace(first_name='Вася', last_name=None, username=None, email='vasya@example.com'),
    )
    send_mock = _patch_email(monkeypatch)
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message='blocked'))

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    send_mock.assert_called_once()
    assert send_mock.call_args.args[0] == 'vasya@example.com'
    # файл не скачался (autouse-мок) — письмо уходит без вложений, только со ссылкой
    assert send_mock.call_args.args[4] is None


async def test_telegram_rejected_file_falls_back_to_email_with_attachment(monkeypatch):
    """Telegram отверг файл — ссылка в чате не считается доставкой чека.

    Регрессия: _deliver молча деградировал до сообщения со ссылкой, флаг
    «доставлено в Telegram» всё равно выставлялся, и email-фоллбек не
    срабатывал. Клиент под VPN оставался с неоткрывающейся ссылкой lknpd,
    хотя целый файл чека лежал у нас на руках.
    """
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    monkeypatch.setattr(
        'app.services.nalogo_service._download_receipt_file',
        AsyncMock(return_value=(b'jpeg-bytes', 'image/jpeg')),
    )
    send_mock = _patch_email(monkeypatch)
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message='PHOTO_INVALID_DIMENSIONS'))

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
        user_email='buyer@example.com',
    )

    # ссылка в Telegram ушла, но сам файл — нет, поэтому догоняем письмом с вложением
    assert bot.send_message.await_count == 1
    send_mock.assert_called_once()
    assert send_mock.call_args.args[4] == [('receipt_uuid-1.jpg', b'jpeg-bytes', 'image/jpeg')]


async def test_delivered_to_telegram_skips_email(monkeypatch):
    """Чек дошёл в Telegram — письмо не дублируем."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    _patch_user_lookup(monkeypatch, None)
    send_mock = _patch_email(monkeypatch)
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
        user_email='buyer@example.com',
    )

    assert bot.send_message.await_count == 1
    send_mock.assert_not_called()


async def test_email_not_sent_when_smtp_unconfigured(monkeypatch):
    """SMTP не настроен — не падаем, чек остаётся хотя бы в админ-топике."""
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500', raising=False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', None, raising=False)
    send_mock = _patch_email(monkeypatch, configured=False)
    bot = _bot()

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=None,
        user_email='buyer@example.com',
    )

    send_mock.assert_not_called()
    assert bot.send_message.await_count == 1  # админ-топик


@pytest.mark.parametrize(
    'error',
    [
        TelegramNotFound(method=MagicMock(), message='chat not found'),
        TelegramRetryAfter(method=MagicMock(), message='Too Many Requests', retry_after=30),
    ],
    ids=['chat_not_found', 'flood_control'],
)
async def test_routine_delivery_failures_are_warnings_not_errors(monkeypatch, error):
    """«Чат не найден» и флуд-контроль — штатные исходы рассылки, не сбои кода.

    Обе ошибки не входили в список транзиентных, поэтому рутинные ситуации
    (пользователь не нажимал Start; 429 от Telegram) логировались как ERROR
    с трейсбеком и зашумляли алерты.
    """
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None, raising=False)
    _patch_user_lookup(monkeypatch, None)
    _patch_email(monkeypatch, configured=False)
    log = MagicMock()
    monkeypatch.setattr(_nalogo_module, 'logger', log)
    bot = _bot()
    bot.send_message = AsyncMock(side_effect=error)

    await send_nalogo_receipt_notifications(
        bot=bot,
        nalogo_service=_nalogo(),
        receipt_uuid='uuid-1',
        amount_kopeks=10000,
        telegram_user_id=111,
    )

    warned = [c for c in log.warning.call_args_list if 'транзиент' in c.args[0]]
    assert warned, f'{type(error).__name__} должен логироваться как транзиент'
    assert warned[0].kwargs['error_type'] == type(error).__name__
    assert not [c for c in log.error.call_args_list if 'Ошибка отправки чека' in c.args[0]]
