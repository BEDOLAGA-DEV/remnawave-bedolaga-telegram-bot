from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.services import nalogo_service


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('attachment', [None, (b'fake-pdf', 'application/pdf'), (b'fake-image', 'image/png')])
async def test_receipt_delivery_localizes_user_separately_from_admin(monkeypatch, language, attachment):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    user = SimpleNamespace(
        language=language, email='buyer@receipt.example', first_name='<Example>', last_name=None, username='sample'
    )
    monkeypatch.setattr('app.database.database.AsyncSessionLocal', Session)
    monkeypatch.setattr('app.database.crud.user.get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'en')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '-100500')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', 77)
    monkeypatch.setattr(nalogo_service, '_download_receipt_file', AsyncMock(return_value=attachment))
    email = AsyncMock()
    monkeypatch.setattr(nalogo_service, '_send_receipt_email', email)
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock())
    url = 'https://receipt.example/print'
    await nalogo_service.send_nalogo_receipt_notifications(
        bot,
        SimpleNamespace(get_receipt_print_url=lambda _: url),
        'masked-1',
        12345,
        123,
        context_label='Operator context',
    )
    sender = (
        bot.send_message
        if attachment is None
        else bot.send_document
        if attachment[1] == 'application/pdf'
        else bot.send_photo
    )
    user_call, admin_call = sender.await_args_list
    field = 'text' if attachment is None else 'caption'
    assert user_call.kwargs[field] == get_texts(language).t('NALOGO_USER_RECEIPT', '').format(
        amount=settings.format_price(12345)
    )
    assert user_call.kwargs['reply_markup'].inline_keyboard[0][0].text == get_texts(language).t(
        'NALOGO_OPEN_RECEIPT', ''
    )
    assert admin_call.kwargs['reply_markup'].inline_keyboard[0][0].text == get_texts('en').t('NALOGO_OPEN_RECEIPT', '')
    assert admin_call.kwargs['message_thread_id'] == 77
    assert 'Operator context' in admin_call.kwargs[field]
    assert '&lt;Example&gt;' in admin_call.kwargs[field]
    assert user_call.kwargs['reply_markup'].inline_keyboard[0][0].url == url
    assert user_call.kwargs['parse_mode'] == 'HTML'
    email.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_admin_language_and_explicit_email_fallback(monkeypatch):
    class Session:
        active = False

        async def __aenter__(self):
            Session.active = True
            return self

        async def __aexit__(self, *args):
            Session.active = False
            return False

    class User:
        email = 'stored@receipt.example'
        first_name = 'Example'
        last_name = None
        username = 'example'

        @property
        def language(self):
            assert Session.active
            return 'fa'

    async def lookup(session, telegram_id):
        return User() if telegram_id == 123 else SimpleNamespace(language='zh')

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', Session)
    monkeypatch.setattr('app.database.crud.user.get_user_by_telegram_id', AsyncMock(side_effect=lookup))
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'en')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', '456')
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID', None)
    monkeypatch.setattr(nalogo_service, '_download_receipt_file', AsyncMock(return_value=None))
    email = AsyncMock(return_value=True)
    monkeypatch.setattr(nalogo_service, '_send_receipt_email', email)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError('mock delivery failure'), None]))
    url = 'https://receipt.example/print'
    await nalogo_service.send_nalogo_receipt_notifications(
        bot,
        SimpleNamespace(get_receipt_print_url=lambda _: url),
        'masked-1',
        12345,
        123,
        context_label='<operator & data>',
        user_email='explicit@receipt.example',
    )
    email.assert_awaited_once_with('explicit@receipt.example', settings.format_price(12345), url, None, language='fa')
    admin_call = bot.send_message.await_args
    assert admin_call.kwargs['chat_id'] == 456
    assert admin_call.kwargs['reply_markup'].inline_keyboard[0][0].text == get_texts('zh').t('NALOGO_OPEN_RECEIPT', '')
    assert '&lt;operator &amp; data&gt;' in admin_call.kwargs['text']
    assert '<operator' not in admin_call.kwargs['text']
