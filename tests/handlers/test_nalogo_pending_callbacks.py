"""TG-095: buttons must address the exact pending receipt, including long IDs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import monitoring
from app.services.nalogo_service import NALOGO_PENDING_VERIFICATION_KEY
from tests.services.test_nalogo_pending_verification import _FakeCache, _pending, _service


@pytest.mark.parametrize('action', ['verified', 'retry'])
@pytest.mark.parametrize('long_id', [False, True])
async def test_pending_receipt_button_roundtrip(monkeypatch, action, long_id):
    payment_id = 'a' * 30 + '000001' if not long_id else 'платёж-' * 20
    neighbor_id = 'a' * 30 + '000002' if not long_id else payment_id + '2'
    cache = _FakeCache(
        {NALOGO_PENDING_VERIFICATION_KEY: [_pending(payment_id=payment_id), _pending(payment_id=neighbor_id)]}
    )
    monkeypatch.setattr('app.services.nalogo_service.cache', cache)
    service = _service()
    service.create_receipt = AsyncMock(return_value='receipt-uuid')
    monkeypatch.setattr('app.services.nalogo_service.NaloGoService', lambda: service)
    monkeypatch.setattr('app.services.nalogo_service.send_nalogo_receipt_notifications', AsyncMock())
    callback = SimpleNamespace(
        data='', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock(), bot=object()
    )
    await monitoring.nalogo_pending_callback.__wrapped__(callback)
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    buttons = [
        b for row in keyboard.inline_keyboard for b in row if b.callback_data.startswith(f'admin_nalogo_{action}:')
    ]
    assert len(buttons) == 2
    assert buttons[0].callback_data != buttons[1].callback_data
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    callback.data = buttons[0].callback_data
    monkeypatch.setattr(monitoring, 'nalogo_pending_callback', AsyncMock())
    handler = monitoring.nalogo_mark_verified_callback if action == 'verified' else monitoring.nalogo_retry_callback
    await handler.__wrapped__(callback)
    assert [r['payment_id'] for r in cache.lists[NALOGO_PENDING_VERIFICATION_KEY]] == [neighbor_id]
    assert cache.values[f'nalogo:created:{payment_id}']
    if action == 'retry':
        assert service.create_receipt.await_args.kwargs['payment_id'] == payment_id
    else:
        service.create_receipt.assert_not_awaited()


async def test_legacy_truncated_callback_does_not_choose_a_receipt(monkeypatch):
    service = _service()
    service.mark_pending_as_verified = AsyncMock()
    service.get_pending_verification_receipts = AsyncMock(
        return_value=[_pending(payment_id='a' * 30 + '000001'), _pending(payment_id='a' * 30 + '000002')]
    )
    monkeypatch.setattr('app.services.nalogo_service.NaloGoService', lambda: service)
    callback = SimpleNamespace(data='admin_nalogo_verified:' + 'a' * 30, answer=AsyncMock())
    await monitoring.nalogo_mark_verified_callback.__wrapped__(callback)
    service.mark_pending_as_verified.assert_not_awaited()
    assert 'обнов' in callback.answer.await_args.args[0].lower()


async def test_ambiguous_pending_token_is_rejected(monkeypatch):
    service = _service()
    service.get_pending_verification_receipts = AsyncMock(
        return_value=[_pending(payment_id='first'), _pending(payment_id='second')]
    )
    monkeypatch.setattr(monitoring, '_nalogo_pending_token', lambda _: 'collision')
    assert await monitoring._resolve_nalogo_pending_id(service, 'collision') is None
