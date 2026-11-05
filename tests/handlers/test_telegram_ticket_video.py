"""Telegram ticket handlers must persist video, including an empty caption."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database.models import Ticket, TicketMessage, User
from app.handlers import tickets
from app.handlers.admin import tickets as admin_tickets
from app.services.admin_notification_service import AdminNotificationService
from app.services.event_emitter import event_emitter
from tests.fixtures.sqlite_memory import memory_session


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['create', 'reply', 'admin_reply'])
@pytest.mark.parametrize(
    ('kind', 'caption'),
    [
        ('video', 'Synthetic video caption'),
        ('video', None),
        ('video', 'x'),
        ('photo', 'Synthetic photo caption'),
        ('text', 'Synthetic text without media'),
    ],
)
async def test_ticket_handler_preserves_media(monkeypatch, entry, kind, caption):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: True)
    monkeypatch.setattr(tickets.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(tickets.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(tickets.cache, 'set', AsyncMock())
    monkeypatch.setattr(tickets, '_try_delete_message_later', AsyncMock())
    monkeypatch.setattr(tickets, 'notify_admins_about_new_ticket', AsyncMock())
    monkeypatch.setattr(tickets, 'notify_admins_about_ticket_reply', AsyncMock())
    monkeypatch.setattr(admin_tickets, 'notify_user_about_ticket_reply', AsyncMock())
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())

    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__, TicketMessage.__table__]) as db:
        owner = User(id=1, telegram_id=1, language='ru')
        admin = User(id=2, telegram_id=2, language='ru')
        db.add_all([owner, admin])
        if entry != 'create':
            db.add(Ticket(id=1, user_id=1, title='Synthetic ticket'))
        await db.commit()
        actor = admin if entry == 'admin_reply' else owner
        actor_id = actor.id
        state_value = {
            'create': tickets.TicketStates.waiting_for_message,
            'reply': tickets.TicketStates.waiting_for_reply,
            'admin_reply': admin_tickets.AdminTicketStates.waiting_for_reply,
        }[entry]
        state = SimpleNamespace(
            get_state=AsyncMock(return_value=state_value),
            get_data=AsyncMock(return_value={'title': 'Synthetic ticket', 'ticket_id': 1, 'reply_mode': True}),
            update_data=AsyncMock(),
            clear=AsyncMock(),
        )
        file_id = 'masked_video_1234567890' if kind == 'video' else 'masked_photo_1234567890'
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=actor.telegram_id),
            chat=SimpleNamespace(id=actor.telegram_id),
            message_id=1,
            text=caption if kind == 'text' else None,
            caption=caption if kind != 'text' else None,
            photo=[SimpleNamespace(file_id=file_id)] if kind == 'photo' else [],
            video=SimpleNamespace(file_id=file_id) if kind == 'video' else None,
            answer=AsyncMock(),
            bot=SimpleNamespace(),
        )
        handler = {
            'create': tickets.handle_ticket_message_input,
            'reply': tickets.handle_ticket_reply,
            'admin_reply': admin_tickets.handle_admin_ticket_reply,
        }[entry]
        await handler(message, state, actor, db)
        db.expire_all()
        stored = (await db.execute(select(TicketMessage))).scalar_one_or_none()
        assert stored is not None, 'Ticket handler silently discarded a supported attachment'
        assert stored.message_text == (caption or '')
        assert stored.user_id == actor_id
        assert stored.is_from_admin is (entry == 'admin_reply')
        assert stored.has_media is (kind != 'text')
        assert stored.media_type == (None if kind == 'text' else kind)
        assert stored.media_file_id == (None if kind == 'text' else file_id)
        assert stored.media_caption == (None if kind == 'text' else caption)


class RecordingBot:
    def __init__(self):
        self.sent = []
        self.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))

    async def send_photo(self, **kwargs):
        self.sent.append(('photo', kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_document(self, **kwargs):
        self.sent.append(('document', kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_video(self, **kwargs):
        self.sent.append(('video', kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_media_group(self, *, media, **kwargs):
        assert 2 <= len(media) <= 10, 'Telegram albums require 2–10 items'
        messages = []
        for item in media:
            self.sent.append((item.type, {item.type: item.media, **kwargs}))
            messages.append(SimpleNamespace(message_id=len(self.sent)))
        return messages


def _ticket_with_media(kinds):
    timestamp = datetime.now(UTC)
    return Ticket(
        id=1,
        user_id=1,
        title='Synthetic ticket',
        status='open',
        created_at=timestamp,
        user_reply_block_permanent=False,
        user=User(id=1, telegram_id=1, language='ru'),
        messages=[
            TicketMessage(
                id=i + 1,
                user_id=1,
                ticket_id=1,
                message_text='',
                is_from_admin=False,
                has_media=True,
                media_type=kind,
                media_file_id=f'masked_{kind}_{i:016d}',
                created_at=timestamp,
            )
            for i, kind in enumerate(kinds)
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['user', 'admin'])
@pytest.mark.parametrize(
    'kinds', [('video',), ('photo',), ('photo', 'video'), ('video',) * 11, ('photo', 'document', 'video')]
)
@pytest.mark.parametrize('gallery', [False, True])
async def test_attachment_button_delivers_original_photo_and_video(monkeypatch, side, kinds, gallery):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: True)
    ticket = _ticket_with_media(kinds)
    if gallery:
        ticket.messages[0].media_items = [{'type': m.media_type, 'file_id': m.media_file_id} for m in ticket.messages]
        ticket.messages = ticket.messages[:1]
    monkeypatch.setattr(tickets.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket))
    bot = RecordingBot()
    prefix = 'admin_' if side == 'admin' else ''
    callback = SimpleNamespace(
        data=f'{prefix}ticket_attachments_1',
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(bot=bot),
        answer=AsyncMock(),
    )
    if side == 'admin':
        callbacks = {}
        dispatcher = SimpleNamespace(
            callback_query=SimpleNamespace(register=lambda fn, *args: callbacks.update({fn.__name__: fn})),
            message=SimpleNamespace(register=lambda *args: None),
        )
        admin_tickets.register_handlers(dispatcher)
        handler = callbacks['send_admin_ticket_attachments']
    else:
        handler = tickets.send_ticket_attachments
    await handler(callback, ticket.user, AsyncMock())
    assert [(kind, kwargs[kind]) for kind, kwargs in bot.sent] == [
        (kind, f'masked_{kind}_{i:016d}') for i, kind in enumerate(kinds)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['user', 'admin'])
@pytest.mark.parametrize('gallery_only', [False, True])
async def test_video_ticket_view_exposes_attachments(monkeypatch, side, gallery_only):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: True)
    ticket = _ticket_with_media(['video'])
    if gallery_only:
        ticket.messages[0].media_items = [{'type': 'video', 'file_id': ticket.messages[0].media_file_id}]
        ticket.messages[0].media_type = None
        ticket.messages[0].media_file_id = None
    monkeypatch.setattr(tickets.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket))
    module = tickets if side == 'user' else admin_tickets
    sent = AsyncMock()
    monkeypatch.setattr(module, 'safe_edit_or_resend', sent)
    prefix = 'admin_' if side == 'admin' else ''
    callback = SimpleNamespace(
        data=f'{prefix}view_ticket_1',
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )
    if side == 'admin':
        await module.view_admin_ticket(callback, ticket.user, AsyncMock(), AsyncMock())
    else:
        await module.view_ticket(callback, ticket.user, AsyncMock())
    _, text, keyboard = sent.await_args.args
    assert 'видео' in text
    assert f'{prefix}ticket_attachments_1' in [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['photo', 'video'])
async def test_user_notification_delivers_admin_attachment(monkeypatch, kind):
    ticket = _ticket_with_media([kind])
    ticket.messages[0].is_from_admin = True
    monkeypatch.setattr(admin_tickets.TicketMessageCRUD, 'get_last_message', AsyncMock(return_value=ticket.messages[0]))
    monkeypatch.setattr(admin_tickets.SupportSettingsService, 'get_user_ticket_notifications_enabled', lambda: True)
    bot = RecordingBot()
    await admin_tickets.notify_user_about_ticket_reply(bot, ticket, '<test> & caption', AsyncMock())
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == kind
    assert bot.sent[0][1][kind] == ticket.messages[0].media_file_id
    assert '&lt;test&gt; &amp; caption' in bot.sent[0][1]['caption']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['photo', 'video'])
@pytest.mark.parametrize('text', ['Synthetic caption', 'x' * 1100])
async def test_admin_notification_delivers_attachment_to_ticket_topic(monkeypatch, kind, text):
    monkeypatch.setattr(AdminNotificationService, '_is_enabled', lambda self: True)
    monkeypatch.setattr(AdminNotificationService, '_resolve_topic_id', lambda self, category=None: 7)
    monkeypatch.setattr(admin_tickets.SupportSettingsService, 'get_admin_ticket_notifications_enabled', lambda: True)
    bot = RecordingBot()
    service = AdminNotificationService(bot)
    service.chat_id = 1
    monkeypatch.setattr(service, '_send_message', AsyncMock(return_value=True))
    result = await service.send_ticket_event_notification(
        text, media_type=kind, media_file_id='masked_media_1234567890'
    )
    assert result is True
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == kind
    assert bot.sent[0][1][kind] == 'masked_media_1234567890'
    assert bot.sent[0][1]['message_thread_id'] == 7
    if len(text) > 1024:
        assert 'caption' not in bot.sent[0][1]
        assert service._send_message.await_args.args[0] == text
    else:
        assert bot.sent[0][1]['caption'] == text
