import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.handlers.admin.messages as m


@pytest.mark.asyncio
async def test_capture_stores_copy_ids(monkeypatch):
    captured = {}

    async def update_data(**kw):
        captured.update(kw)

    # show_button_selector is the next step; stub it to isolate the capture.
    monkeypatch.setattr(m, 'show_button_selector', AsyncMock())

    state = SimpleNamespace(
        update_data=AsyncMock(side_effect=update_data),
        set_state=AsyncMock(),
        get_data=AsyncMock(return_value={'broadcast_mode': 'copy'}),
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=777), message_id=4242, content_type='text', answer=AsyncMock()
    )
    db_user = SimpleNamespace(language='ru')

    # The handler is wrapped by @admin_required/@error_handler, which reject a
    # non-aiogram event; unwrap to exercise the capture logic directly.
    raw = inspect.unwrap(m.process_broadcast_copy_source)
    await raw(message, db_user, state)

    assert captured['copy_from_chat_id'] == 777
    assert captured['copy_source_message_id'] == 4242
    assert captured['broadcast_message'] == '📋 Рассылка копией'
    m.show_button_selector.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_rejects_service_message(monkeypatch):
    monkeypatch.setattr(m, 'show_button_selector', AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock(), get_data=AsyncMock(return_value={}))
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1), message_id=2, content_type='pinned_message', answer=AsyncMock()
    )
    db_user = SimpleNamespace(language='ru')

    raw = inspect.unwrap(m.process_broadcast_copy_source)
    await raw(message, db_user, state)

    message.answer.assert_awaited_once()
    m.show_button_selector.assert_not_awaited()
