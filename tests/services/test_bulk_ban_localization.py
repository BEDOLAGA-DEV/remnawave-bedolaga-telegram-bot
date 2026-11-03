from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import bulk_ban_service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'language, marker',
    [('ru', 'заблокирован'), ('en', 'blocked'), ('ua', 'заблоковано'), ('zh', '已被封禁'), ('fa', 'مسدود')],
)
async def test_bulk_ban_localizes_notice_without_changing_audit_reason(monkeypatch, language, marker):
    user = SimpleNamespace(id=7, language=language, status='active')
    monkeypatch.setattr(bulk_ban_service, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(type(bulk_ban_service.settings), 'is_notifications_enabled', lambda self: True)
    service = bulk_ban_service.BulkBanService()
    service.user_service.block_user = AsyncMock(return_value=True)
    bot = SimpleNamespace(send_message=AsyncMock())
    db = object()
    reason = 'Operator <details> & reason'
    result = await service.ban_users_by_telegram_ids(db, 10, [123], reason=reason, bot=bot, notify_admin=False)
    assert result == (1, 0, [])
    service.user_service.block_user.assert_awaited_once_with(db, 7, 10, reason)
    message = bot.send_message.await_args.kwargs['text']
    assert marker in message
    assert 'Operator &lt;details&gt; &amp; reason' in message
