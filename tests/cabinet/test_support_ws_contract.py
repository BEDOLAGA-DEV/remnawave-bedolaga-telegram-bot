from __future__ import annotations

import base64
import hashlib
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.cabinet.routes import support_ws


def _context(user_id: int = 10) -> support_ws.WsUserContext:
    user = types.SimpleNamespace(
        id=user_id,
        telegram_id=1000 + user_id,
        email=None,
        email_verified=False,
        username='mobile_user',
        first_name='Mobile',
        last_name='User',
        status='active',
    )
    return support_ws.WsUserContext(user=user, token_payload={'sub': str(user_id)}, role='owner')


def _session() -> support_ws.SupportWsSession:
    websocket = types.SimpleNamespace()
    return support_ws.SupportWsSession(websocket=websocket, context=_context())


class _FakeDb:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, item) -> None:
        if isinstance(item, support_ws.TicketMessage) and item.id is None:
            item.id = 501
        self.added.append(item)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _item, _attrs=None) -> None:
        return None


def _ticket(**overrides):
    base = {
        'id': 3,
        'user_id': 10,
        'title': 'Need help',
        'status': 'open',
        'priority': 'normal',
        'created_at': datetime(2026, 7, 9, 0, 0, tzinfo=UTC),
        'updated_at': datetime(2026, 7, 9, 0, 1, tzinfo=UTC),
        'closed_at': None,
        'messages': [],
        'user': None,
        'is_user_reply_blocked': False,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _ws_client() -> TestClient:
    app = FastAPI()
    app.include_router(support_ws.router, prefix='/cabinet')
    return TestClient(app)


def test_shared_error_contract_uses_integer_or_null_retry_after() -> None:
    error = support_ws._shared_error('RATE_LIMITED', 'Try later', retry_after_ms=2500)
    assert error['retryAfterMs'] == 2500
    assert isinstance(error['retryAfterMs'], int)
    assert error['details'] == {}
    assert error['backpressure'] is None

    no_retry_after = support_ws._shared_error('VALIDATION_ERROR', 'Invalid')
    assert no_retry_after['retryAfterMs'] is None


def test_support_ws_rejects_query_token_auth() -> None:
    with _ws_client() as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            '/cabinet/ws/support/v1?token=query-token',
            subprotocols=[support_ws.SUPPORTED_SUBPROTOCOL],
        ):
            pass


def test_support_ws_rejects_missing_subprotocol() -> None:
    with _ws_client() as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/cabinet/ws/support/v1', headers={'authorization': 'Bearer token'}):
            pass


def test_support_ws_accepts_bearer_and_echoes_supported_subprotocol(monkeypatch) -> None:
    async def fake_authenticate(db, websocket, *, access_token=None):
        return _context(), None

    monkeypatch.setattr(support_ws, '_authenticate_ws', fake_authenticate)

    with _ws_client() as client:
        with client.websocket_connect(
            '/cabinet/ws/support/v1',
            headers={'authorization': 'Bearer access-token'},
            subprotocols=['legacy', support_ws.SUPPORTED_SUBPROTOCOL],
        ) as websocket:
            assert websocket.accepted_subprotocol == support_ws.SUPPORTED_SUBPROTOCOL
            ready = websocket.receive_json()
            assert ready['event'] == 'connection.ready'
            assert ready['payload']['mediaDownload']['transport'] == 'websocket'


def test_ticket_snapshot_keeps_assignment_fields_explicitly_nullable() -> None:
    message = types.SimpleNamespace(
        id=7,
        ticket_id=3,
        user_id=10,
        is_from_admin=False,
        message_text='hello',
        media_file_id=None,
        media_items=None,
        created_at=datetime(2026, 7, 9, 0, 0, tzinfo=UTC),
    )
    ticket = types.SimpleNamespace(
        id=3,
        title='Need help',
        status='open',
        priority='normal',
        created_at=datetime(2026, 7, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 9, 0, 1, tzinfo=UTC),
        closed_at=None,
        messages=[message],
        user=None,
    )

    snapshot = support_ws._ticket_snapshot(ticket, include_messages=True)

    assert snapshot['assignedTo'] is None
    assert snapshot['messages'][0]['attachments'] == []


@pytest.mark.asyncio
async def test_owner_ticket_reply_respects_reply_block(monkeypatch) -> None:
    session = _session()
    ticket = _ticket(user_id=session.context.user_id, is_user_reply_blocked=True)
    db = _FakeDb()

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)

    with pytest.raises(PermissionError, match='Replies to this ticket are blocked'):
        await support_ws._handle_ticket_reply(
            db,
            session,
            {
                'ticketId': str(ticket.id),
                'clientMessageId': 'client-1',
                'body': 'blocked reply',
                'attachmentMediaIds': [],
                'idempotencyKey': 'reply-1',
            },
        )

    assert db.added == []
    assert session.idempotency == {}


@pytest.mark.asyncio
async def test_owner_ws_reply_notifies_legacy_admin_websocket(monkeypatch) -> None:
    session = _session()
    ticket = _ticket(user_id=session.context.user_id)
    legacy_notify = AsyncMock()

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)
    monkeypatch.setattr(
        support_ws.TicketNotificationCRUD, 'create_admin_notification_for_user_reply', AsyncMock(return_value=object())
    )
    monkeypatch.setattr(support_ws, 'notify_admins_ticket_reply', legacy_notify)
    monkeypatch.setattr(support_ws.support_ws_manager, 'broadcast_ticket_event', AsyncMock())

    await support_ws._handle_ticket_reply(
        _FakeDb(),
        session,
        {
            'ticketId': str(ticket.id),
            'clientMessageId': 'client-1',
            'body': 'hello legacy admins',
            'attachmentMediaIds': [],
            'idempotencyKey': 'reply-1',
        },
    )

    legacy_notify.assert_awaited_once_with(ticket.id, 'hello legacy admins', session.context.user_id)


@pytest.mark.asyncio
async def test_support_ws_reply_notifies_legacy_user_websocket(monkeypatch) -> None:
    session = _session()
    session.context.role = 'support'
    session.context.user.id = 20
    session.websocket = types.SimpleNamespace(
        headers={'user-agent': 'pytest', 'x-forwarded-for': '203.0.113.9'},
        client=types.SimpleNamespace(host='127.0.0.1'),
    )
    ticket = _ticket(user_id=10)
    legacy_notify = AsyncMock()

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    async def fake_check_permission(_db, _user, permission, ip_address=None):
        return permission == 'tickets:reply', 'ok'

    async def fake_log_action(_db, **_kwargs):
        return None

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)
    monkeypatch.setattr(support_ws.PermissionService, 'check_permission', fake_check_permission)
    monkeypatch.setattr(support_ws.PermissionService, 'log_action', fake_log_action)
    monkeypatch.setattr(
        support_ws.TicketNotificationCRUD, 'create_user_notification_for_admin_reply', AsyncMock(return_value=object())
    )
    monkeypatch.setattr(support_ws, 'notify_user_ticket_reply', legacy_notify)
    monkeypatch.setattr(support_ws.support_ws_manager, 'broadcast_ticket_event', AsyncMock())

    await support_ws._handle_ticket_reply(
        _FakeDb(),
        session,
        {
            'ticketId': str(ticket.id),
            'clientMessageId': 'client-1',
            'body': 'hello legacy user',
            'attachmentMediaIds': [],
            'idempotencyKey': 'reply-1',
        },
    )

    legacy_notify.assert_awaited_once_with(ticket.user_id, ticket.id, 'hello legacy user')


@pytest.mark.asyncio
async def test_privileged_ws_reply_writes_permission_audit_without_sensitive_payload(monkeypatch) -> None:
    session = _session()
    session.context.role = 'support'
    session.context.user.id = 20
    session.websocket = types.SimpleNamespace(
        headers={'user-agent': 'pytest', 'x-forwarded-for': '203.0.113.9'},
        client=types.SimpleNamespace(host='127.0.0.1'),
    )
    ticket = _ticket(user_id=10)
    audit_rows = []

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    async def fake_check_permission(_db, _user, permission, ip_address=None):
        assert permission == 'tickets:reply'
        assert ip_address == '203.0.113.9'
        return True, 'ok'

    async def fake_log_action(_db, **kwargs):
        audit_rows.append(kwargs)

    async def fake_notify(_db, _ticket, _body):
        return None

    async def fake_broadcast(_db, _ticket, _event):
        return None

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)
    monkeypatch.setattr(support_ws.PermissionService, 'check_permission', fake_check_permission)
    monkeypatch.setattr(support_ws.PermissionService, 'log_action', fake_log_action)
    monkeypatch.setattr(support_ws.TicketNotificationCRUD, 'create_user_notification_for_admin_reply', fake_notify)
    monkeypatch.setattr(support_ws.support_ws_manager, 'broadcast_ticket_event', fake_broadcast)

    await support_ws._handle_ticket_reply(
        _FakeDb(),
        session,
        {
            'ticketId': str(ticket.id),
            'clientMessageId': 'client-1',
            'body': 'sensitive support response',
            'attachmentMediaIds': [],
            'idempotencyKey': 'reply-1',
        },
    )

    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row['action'] == 'tickets:reply'
    assert row['resource_type'] == 'tickets'
    assert row['resource_id'] == str(ticket.id)
    assert row['status'] == 'success'
    assert row['request_method'] == 'WEBSOCKET'
    assert row['request_path'] == '/cabinet/ws/support/v1'
    assert row['details']['command'] == 'ticket.reply'
    assert row['details']['hasBody'] is True
    assert row['details']['attachmentCount'] == 0
    assert 'sensitive support response' not in str(row['details'])
    assert 'request_body' not in row['details']


@pytest.mark.asyncio
async def test_privileged_ws_status_update_writes_permission_audit(monkeypatch) -> None:
    session = _session()
    session.context.role = 'admin'
    session.context.user.id = 20
    session.websocket = types.SimpleNamespace(
        headers={'user-agent': 'pytest', 'x-forwarded-for': '203.0.113.10'},
        client=types.SimpleNamespace(host='127.0.0.1'),
    )
    ticket = _ticket(user_id=10)
    audit_rows = []

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    async def fake_check_permission(_db, _user, permission, ip_address=None):
        assert permission == 'tickets:close'
        assert ip_address == '203.0.113.10'
        return True, 'ok'

    async def fake_log_action(_db, **kwargs):
        audit_rows.append(kwargs)

    async def fake_broadcast(_db, _ticket, _event):
        return None

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)
    monkeypatch.setattr(support_ws.PermissionService, 'check_permission', fake_check_permission)
    monkeypatch.setattr(support_ws.PermissionService, 'log_action', fake_log_action)
    monkeypatch.setattr(support_ws.support_ws_manager, 'broadcast_ticket_event', fake_broadcast)

    db = _FakeDb()
    result = await support_ws._handle_ticket_mutation(
        db,
        session,
        {'ticketId': str(ticket.id), 'status': 'closed', 'idempotencyKey': 'status-1'},
        command_name='ticket.status.update',
        field_name='status',
        allowed_values=support_ws.ALLOWED_STATUSES,
        event_name='ticket.status.updated',
    )

    assert result['ticket']['status'] == 'closed'
    assert db.commits == 1
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row['action'] == 'tickets:close'
    assert row['resource_type'] == 'tickets'
    assert row['resource_id'] == str(ticket.id)
    assert row['status'] == 'success'
    assert row['request_method'] == 'WEBSOCKET'
    assert row['request_path'] == '/cabinet/ws/support/v1'
    assert row['details'] == {
        'method': 'WEBSOCKET',
        'path': '/cabinet/ws/support/v1',
        'command': 'ticket.status.update',
        'ticketId': str(ticket.id),
        'field': 'status',
        'previousValue': 'open',
        'newValue': 'closed',
    }


@pytest.mark.asyncio
async def test_upload_lifecycle_makes_media_attachable_only_after_finish(monkeypatch) -> None:
    session = _session()
    data = b'hello websocket media'
    digest = hashlib.sha256(data).hexdigest()

    async def fake_upload_to_telegram(upload: support_ws.UploadTransfer) -> dict:
        assert bytes(upload.chunks) == data
        return {
            'mediaId': 'telegram-file-id',
            'fileUniqueId': 'unique-id',
            'type': upload.media_type,
            'fileName': upload.file_name,
            'contentType': upload.content_type,
            'sizeBytes': len(upload.chunks),
            'caption': None,
        }

    monkeypatch.setattr(support_ws, '_upload_to_telegram', fake_upload_to_telegram)

    ticket = _ticket(user_id=session.context.user_id)

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)

    begin = await support_ws._handle_upload_begin(
        _FakeDb(),
        session,
        {
            'ticketId': str(ticket.id),
            'fileName': 'proof.png',
            'contentType': 'image/png',
            'mediaType': 'photo',
            'sizeBytes': len(data),
        },
    )
    assert session.completed_media == {}

    chunk = await support_ws._handle_upload_chunk(
        session,
        {
            'uploadId': begin['uploadId'],
            'offset': 0,
            'data': base64.b64encode(data).decode(),
        },
    )
    assert chunk['receivedBytes'] == len(data)

    finish = await support_ws._handle_upload_finish(
        session,
        {'uploadId': begin['uploadId'], 'checksumSha256': digest},
    )
    assert finish['mediaId'] == 'telegram-file-id'
    assert session.completed_media['telegram-file-id']['sha256'] == digest


@pytest.mark.asyncio
async def test_upload_finish_rejects_checksum_mismatch(monkeypatch) -> None:
    session = _session()
    data = b'corrupted'
    ticket = _ticket(user_id=session.context.user_id)

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)

    begin = await support_ws._handle_upload_begin(
        _FakeDb(),
        session,
        {
            'ticketId': str(ticket.id),
            'fileName': 'safe.txt',
            'contentType': 'text/plain',
            'mediaType': 'document',
            'sizeBytes': len(data),
        },
    )
    await support_ws._handle_upload_chunk(
        session,
        {
            'uploadId': begin['uploadId'],
            'offset': 0,
            'data': base64.b64encode(data).decode(),
        },
    )

    with pytest.raises(RuntimeError, match='UPLOAD_CHECKSUM_MISMATCH'):
        await support_ws._handle_upload_finish(session, {'uploadId': begin['uploadId'], 'checksumSha256': 'bad'})


@pytest.mark.asyncio
async def test_upload_begin_requires_ticket_id() -> None:
    """Uploads must be anchored to a visible ticket; a ticketless begin is rejected
    so no account can stage arbitrary files through the support bot unauthorized."""
    session = _session()
    with pytest.raises(ValueError, match='ticketId'):
        await support_ws._handle_upload_begin(
            _FakeDb(),
            session,
            {'fileName': 'proof.png', 'contentType': 'image/png', 'mediaType': 'photo', 'sizeBytes': 3},
        )


@pytest.mark.asyncio
async def test_upload_begin_enforces_active_transfer_cap(monkeypatch) -> None:
    """A single session cannot pin unbounded memory by opening endless transfers;
    once MAX_ACTIVE_TRANSFERS live transfers exist, further begins are rate-limited."""
    session = _session()
    ticket = _ticket(user_id=session.context.user_id)

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)

    for _ in range(support_ws.MAX_ACTIVE_TRANSFERS):
        await support_ws._handle_upload_begin(
            _FakeDb(),
            session,
            {
                'ticketId': str(ticket.id),
                'fileName': 'a.png',
                'contentType': 'image/png',
                'mediaType': 'photo',
                'sizeBytes': 3,
            },
        )

    with pytest.raises(RuntimeError, match='RATE_LIMITED'):
        await support_ws._handle_upload_begin(
            _FakeDb(),
            session,
            {
                'ticketId': str(ticket.id),
                'fileName': 'b.png',
                'contentType': 'image/png',
                'mediaType': 'photo',
                'sizeBytes': 3,
            },
        )


def test_ticket_create_declared_out_of_scope_in_ready_event() -> None:
    event = support_ws._message_event(
        'connection.ready',
        {
            'ticketCreate': {'supported': False, 'reason': 'mobile_admin_support_scope_excludes_ticket_create'},
            'mediaDownload': {'transport': 'websocket'},
            'assignment': {'assignedTo': None, 'previousAssignedTo': None},
        },
    )

    assert event['payload']['ticketCreate']['supported'] is False
    assert event['payload']['mediaDownload']['transport'] == 'websocket'
    assert event['payload']['assignment']['assignedTo'] is None
    assert event['payload']['assignment']['previousAssignedTo'] is None


@pytest.mark.asyncio
async def test_ws_reply_idempotent_retry_replays_without_duplicate(monkeypatch) -> None:
    """An identical retry under the same idempotencyKey must replay the cached
    result — not create a second message or re-broadcast. PKCS-style dedup was
    previously broken: _check_idempotency only detected payload conflicts."""
    session = _session()
    ticket = _ticket(user_id=session.context.user_id)
    broadcast = AsyncMock()

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)
    monkeypatch.setattr(
        support_ws.TicketNotificationCRUD, 'create_admin_notification_for_user_reply', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(support_ws, '_notify_ticket_reply_via_telegram', AsyncMock())
    monkeypatch.setattr(support_ws.support_ws_manager, 'broadcast_ticket_event', broadcast)

    db = _FakeDb()
    payload = {
        'ticketId': str(ticket.id),
        'body': 'hi',
        'attachmentMediaIds': [],
        'idempotencyKey': 'reply-dup',
    }
    first = await support_ws._handle_ticket_reply(db, session, dict(payload))
    second = await support_ws._handle_ticket_reply(db, session, dict(payload))

    assert first == second
    assert broadcast.await_count == 1
    assert sum(isinstance(item, support_ws.TicketMessage) for item in db.added) == 1


@pytest.mark.asyncio
async def test_ws_owner_reply_sets_open_status_and_resets_sla(monkeypatch) -> None:
    """A user (owner) reply must move the ticket to 'open' and clear the SLA
    reminder marker, matching TicketCRUD.add_message. The handler previously set
    'pending' and never reset last_sla_reminder_at."""
    session = _session()
    ticket = _ticket(
        user_id=session.context.user_id,
        status='answered',
        last_sla_reminder_at=datetime(2026, 7, 9, tzinfo=UTC),
    )

    async def fake_get_visible_ticket(_db, _context, _ticket_id):
        return ticket

    monkeypatch.setattr(support_ws, '_get_visible_ticket', fake_get_visible_ticket)
    monkeypatch.setattr(
        support_ws.TicketNotificationCRUD, 'create_admin_notification_for_user_reply', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(support_ws, '_notify_ticket_reply_via_telegram', AsyncMock())
    monkeypatch.setattr(support_ws.support_ws_manager, 'broadcast_ticket_event', AsyncMock())

    await support_ws._handle_ticket_reply(
        _FakeDb(),
        session,
        {'ticketId': str(ticket.id), 'body': 'more info', 'attachmentMediaIds': [], 'idempotencyKey': 'k'},
    )

    assert ticket.status == 'open'
    assert ticket.last_sla_reminder_at is None
