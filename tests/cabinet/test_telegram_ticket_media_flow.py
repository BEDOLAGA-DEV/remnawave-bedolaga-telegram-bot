"""Ticket media HTTP roundtrip with real SQLite and a local Telegram file boundary."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import bot_factory
from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import admin_tickets, media, tickets
from app.database.models import Ticket, TicketMessage, User
from app.handlers.admin import tickets as admin_handlers
from app.services.event_emitter import event_emitter
from tests.fixtures.sqlite_memory import memory_session


class LocalTelegramFiles:
    """Capture upload bytes and return them on download; never contact Telegram."""

    def __init__(self):
        self.files = {}
        self.session = SimpleNamespace(close=AsyncMock())
        self.delete_message = AsyncMock()

    def _store(self, upload, kind):
        file_id = f'masked_{kind}_{len(self.files):016d}'
        self.files[file_id] = (upload.filename, upload.data)
        item = SimpleNamespace(file_id=file_id, file_unique_id=f'unique_{file_id}')
        return SimpleNamespace(message_id=len(self.files), photo=[item], video=item, document=item)

    async def send_photo(self, *, photo, **kwargs):
        return self._store(photo, 'photo')

    async def send_video(self, *, video, **kwargs):
        return self._store(video, 'video')

    async def send_document(self, *, document, **kwargs):
        return self._store(document, 'document')

    async def get_file(self, file_id):
        filename, _ = self.files[file_id]
        return SimpleNamespace(file_path=f'{file_id}/{filename}')

    async def download_file(self, file_path):
        file_id = file_path.split('/')[0]
        return BytesIO(self.files[file_id][1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('kind', 'gallery'), [('photo', False), ('video', False), ('photo', True), ('video', True), ('mixed', True)]
)
async def test_upload_create_reply_reload_download(monkeypatch, kind, gallery):
    """Both cabinet sides preserve IDs and bytes; downloads require file-bound tokens."""
    bot = LocalTelegramFiles()
    monkeypatch.setattr(media, 'create_bot', lambda: bot)
    monkeypatch.setattr(bot_factory, 'create_bot', lambda: bot)
    monkeypatch.setattr(media, '_resolve_target_chat_id', lambda: 1)
    monkeypatch.setattr(type(media.settings), 'get_cabinet_jwt_secret', lambda self: 'masked-test-secret')
    monkeypatch.setattr(tickets.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    monkeypatch.setattr(tickets, 'notify_admins_about_new_ticket', AsyncMock())
    monkeypatch.setattr(tickets, 'notify_admins_about_ticket_reply', AsyncMock())
    monkeypatch.setattr(admin_handlers, 'notify_user_about_ticket_reply', AsyncMock())
    for name in (
        'create_admin_notification_for_new_ticket',
        'create_admin_notification_for_user_reply',
        'create_user_notification_for_admin_reply',
    ):
        monkeypatch.setattr(tickets.TicketNotificationCRUD, name, AsyncMock(return_value=None))

    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__, TicketMessage.__table__]) as db:
        owner = User(id=1, first_name='Owner')
        admin = User(id=2, first_name='Admin')
        outsider = User(id=3, first_name='Other')
        db.add_all([owner, admin, outsider])
        await db.commit()
        sessions = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)

        async def request_db():
            # A new identity map per HTTP request makes reload a real DB read.
            async with sessions() as request_session:
                yield request_session

        app = FastAPI()
        for router in (media.router, tickets.router, admin_tickets.router):
            app.include_router(router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = request_db
        app.dependency_overrides[get_current_cabinet_user] = lambda: owner
        for route in admin_tickets.router.routes:
            for dependency in route.dependant.dependencies:
                if dependency.name == 'admin':
                    app.dependency_overrides[dependency.call] = lambda: admin

        kinds = ['photo', 'document', 'video'] if kind == 'mixed' else [kind] * (2 if gallery else 1)
        kind = kinds[0]
        contents = [f'masked-content-{i}'.encode() for i in range(len(kinds))]
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://cabinet') as client:
            uploads = []
            for item_kind, content in zip(kinds, contents, strict=True):
                filename, mime = {
                    'photo': ('masked.png', 'image/png'),
                    'video': ('masked.mp4', 'video/mp4'),
                    'document': ('masked.txt', 'text/plain'),
                }[item_kind]
                upload = await client.post(
                    '/cabinet/media/upload',
                    files={'file': (filename, content, mime)},
                    data={'media_type': item_kind},
                )
                assert upload.status_code == 201, upload.text
                uploads.append(upload.json())
                staged = await client.get(upload.json()['media_url'])
                assert staged.status_code == 200 and staged.content == content

            payload = {'media_type': kind, 'media_file_id': uploads[0]['file_id']}
            if gallery:
                payload['media_items'] = [
                    {'type': item_kind, 'file_id': item['file_id']}
                    for item_kind, item in zip(kinds, uploads, strict=True)
                ]
            created = await client.post(
                '/cabinet/tickets', json={'title': 'Media check', 'message': 'Synthetic attachment', **payload}
            )
            assert created.status_code == 200, created.text
            ticket_id = created.json()['id']
            reply = await client.post(
                f'/cabinet/tickets/{ticket_id}/messages', json={'message': 'Owner reply', **payload}
            )
            assert reply.status_code == 200, reply.text
            admin_reply = await client.post(
                f'/cabinet/admin/tickets/{ticket_id}/reply', json={'message': 'Admin reply', **payload}
            )
            assert admin_reply.status_code == 200, admin_reply.text

            for prefix in ('/cabinet/tickets', '/cabinet/admin/tickets'):
                detail = await client.get(f'{prefix}/{ticket_id}')
                assert detail.status_code == 200, detail.text
                messages = detail.json()['messages']
                assert len(messages) == 3
                assert [message['is_from_admin'] for message in messages] == [False, False, True]
                for message in messages:
                    assert message['has_media'] and message['media_type'] == kind
                    assert message['media_file_id'] == uploads[0]['file_id']
                    items = (
                        message['media_items']
                        if gallery
                        else [
                            {
                                'file_id': message['media_file_id'],
                                'token': message['media_token'],
                            }
                        ]
                    )
                    assert len(items) == len(uploads)
                    if gallery:
                        assert [item['type'] for item in items] == kinds
                    for item, content in zip(items, contents, strict=True):
                        download = await client.get(
                            f'/cabinet/media/{item["file_id"]}', params={'token': item['token']}
                        )
                        assert download.status_code == 200 and download.content == content
                        assert download.headers['cache-control'] == 'private, no-store'
                        expected_type = 'image/png' if item.get('type', kind) == 'photo' else 'application/octet-stream'
                        assert download.headers['content-type'] == expected_type
                        unsigned = await client.get(f'/cabinet/media/{item["file_id"]}')
                        assert unsigned.status_code == 404

            app.dependency_overrides[get_current_cabinet_user] = lambda: outsider
            denied = await client.get(f'/cabinet/tickets/{ticket_id}')
            assert denied.status_code == 404
            assert bot.delete_message.await_count == len(uploads)
