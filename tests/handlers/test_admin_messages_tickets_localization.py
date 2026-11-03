"""Admin broadcasts keep recipient language and operator content across DB expiry."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.admin import messages, tickets
from app.localization.texts import get_texts


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_select_broadcast_target_uses_admin_language(monkeypatch, language):
    count = AsyncMock(return_value=42)
    monkeypatch.setattr(messages, 'get_target_users_count', count)
    callback = SimpleNamespace(
        data='broadcast_no_sub', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    db = MagicMock()
    await inspect.unwrap(messages.select_broadcast_target)(callback, SimpleNamespace(language=language), state, db)
    count.assert_awaited_once_with(db, 'no')
    state.update_data.assert_awaited_once_with(broadcast_target='no')
    rendered = callback.message.edit_text.await_args.args[0]
    assert get_texts(language).t('ADMIN_MESSAGES_UI_SELECT_BROADCAST_TARGET_044', '') in rendered
    assert '42' in rendered


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_target_and_media_labels_use_requested_language(language):
    texts = get_texts(language)
    assert messages.get_target_name('active', language) == texts.t('ADMIN_MESSAGES_UI_SELECT_BROADCAST_TARGET_042', '')
    assert messages.get_target_display_name('tariff_7', language) == texts.t(
        'ADMIN_MESSAGES_UI_GET_TARGET_NAME_105', ''
    ).format(value1='7')
    assert messages.get_target_name('operator-segment', language) == 'operator-segment'
    assert messages._media_label('video', texts) == texts.t('ADMIN_MESSAGES_UI_CONFIRM_BUTTON_SELECTION_069', '')
    assert messages._media_label('operator-media', texts) == 'operator-media'


@pytest.mark.asyncio
@pytest.mark.parametrize('has_media', [False, True])
async def test_broadcast_snapshots_recipient_languages_before_commit(monkeypatch, has_media):
    expired = False

    class Recipient:
        def __init__(self, number, language):
            self.number = number
            self.locale = language

        @property
        def telegram_id(self):
            assert not expired, 'recipient ORM accessed after commit'
            return self.number

        @property
        def language(self):
            assert not expired, 'recipient language accessed after commit'
            return self.locale

    recipients = [Recipient(i + 1, language) for i, language in enumerate(['ru', 'en', 'ua', 'zh', 'fa', None])]
    monkeypatch.setattr(messages, 'get_target_users', AsyncMock(return_value=recipients))
    monkeypatch.setattr(messages, 'safe_edit_or_send_text', AsyncMock())
    monkeypatch.setattr(messages, '_persist_broadcast_result', AsyncMock())
    captured_languages = []

    def keyboard_factory(selected, language):
        captured_languages.append(language)
        return language  # Sentinel accepted by the mocked sender.

    monkeypatch.setattr(messages, 'create_broadcast_keyboard', keyboard_factory)
    monkeypatch.setattr(messages.settings, 'DEFAULT_LANGUAGE', 'en')
    db = MagicMock()

    async def commit():
        nonlocal expired
        expired = True

    async def refresh(history):
        history.id = 7

    db.commit = AsyncMock(side_effect=commit)
    db.refresh = AsyncMock(side_effect=refresh)
    bot = SimpleNamespace(
        send_message=AsyncMock(), send_photo=AsyncMock(), send_video=AsyncMock(), send_document=AsyncMock()
    )
    callback = SimpleNamespace(
        bot=bot, message=SimpleNamespace(chat=SimpleNamespace(id=999), edit_text=AsyncMock()), answer=AsyncMock()
    )
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value=dict(
                broadcast_target='all',
                broadcast_message='Operator <b>original</b>',
                selected_buttons=['balance'],
                has_media=has_media,
                media_type='photo',
                media_file_id='test-file',
            )
        ),
        clear=AsyncMock(),
    )
    admin = SimpleNamespace(id=10, full_name='Admin', telegram_id=999, language='fa')
    await inspect.unwrap(messages.confirm_broadcast)(callback, admin, state, db)
    sender = bot.send_photo if has_media else bot.send_message
    calls = {call.kwargs['chat_id']: call.kwargs for call in sender.await_args_list if call.kwargs['chat_id'] != 999}
    assert len(calls) == 6
    for user_id, language in enumerate(['ru', 'en', 'ua', 'zh', 'fa', 'en'], start=1):
        assert calls[user_id]['reply_markup'] == language
        assert calls[user_id]['caption' if has_media else 'text'] == 'Operator <b>original</b>'
    assert sorted(captured_languages) == ['en', 'fa', 'ru', 'ua', 'zh']
    assert messages._persist_broadcast_result.await_args.kwargs == dict(
        broadcast_id=7, sent_count=6, failed_count=0, status='completed', blocked_count=0
    )
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_ticket_reply_uses_recipient_language_and_escapes_operator_text(monkeypatch, language):
    monkeypatch.setattr(
        tickets.SupportSettingsService, 'get_user_ticket_notifications_enabled', staticmethod(lambda: True)
    )
    monkeypatch.setattr(tickets.TicketMessageCRUD, 'get_last_message', AsyncMock(return_value=None))
    bot = SimpleNamespace(send_message=AsyncMock())
    ticket = SimpleNamespace(id=7, user=SimpleNamespace(telegram_id=123, language=language))
    await tickets.notify_user_about_ticket_reply(bot, ticket, 'Operator <config> & value', AsyncMock())
    kwargs = bot.send_message.await_args.kwargs
    assert 'Operator &lt;config&gt; &amp; value' in kwargs['text']
    assert kwargs['reply_markup'].inline_keyboard[0][0].text == get_texts(language).t('VIEW_TICKET', '')
    assert kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'view_ticket_7'
