"""Regression: refreshing the admin user-messages list after a delete raised
RuntimeError('method is not mounted to a any bot instance').

delete_message_confirm re-invoked list_user_messages with a hand-built
types.CallbackQuery (no bot bound) which then called callback.answer() → crash
(and a double-answer on the same callback.id). The list rendering was extracted
into _render_user_messages_list(message, ...) which never touches a callback;
callers own the single callback.answer().
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import app.handlers.admin.user_messages as um


async def test_render_list_edits_message_and_never_answers():
    msg = MagicMock()
    msg.edit_text = AsyncMock()

    with patch.object(um, 'get_all_user_messages', AsyncMock(return_value=[])):
        await um._render_user_messages_list(msg, MagicMock(), 'ru', 0)

    # renders into the (bot-bound) message, no callback involved → can't crash/double-answer
    msg.edit_text.assert_awaited_once()


def _unwrap(fn):
    """Strip @admin_required / @error_handler to reach the raw handler."""
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


async def test_delete_confirm_renders_via_helper_and_answers_once(monkeypatch):
    monkeypatch.setattr(um, 'delete_user_message', AsyncMock(return_value=True))
    render_mock = AsyncMock()
    monkeypatch.setattr(um, '_render_user_messages_list', render_mock)

    callback = MagicMock()
    callback.data = 'delete_message_confirm:5'
    callback.message = MagicMock()
    callback.answer = AsyncMock()
    db_user = MagicMock(language='ru')

    await _unwrap(um.delete_message_confirm)(callback, db_user, MagicMock())

    # exactly one answer (no second answer on the same callback.id) …
    callback.answer.assert_awaited_once()
    # … and the list is rendered via the helper into the real bound message.
    render_mock.assert_awaited_once()
    assert render_mock.await_args.args[0] is callback.message
