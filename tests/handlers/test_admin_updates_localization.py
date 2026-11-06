"""Update UI selects account language without making real GitHub requests."""

import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import updates as ui
from app.localization.texts import get_texts


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('available', [False, True])
async def test_update_status_uses_account_language_and_same_controls(monkeypatch, language, available):
    service = SimpleNamespace(
        repo='example/project',
        get_version_info=AsyncMock(
            return_value=dict(current_version='1.2.3', has_updates=available, total_newer=2, last_check=None)
        ),
    )
    monkeypatch.setattr(ui, 'version_service', service)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await inspect.unwrap(ui.show_updates_menu)(callback, SimpleNamespace(language=language), None)
    text = callback.message.edit_text.await_args.args[0]
    assert '<code>1.2.3</code>' in text and 'example/project' in text
    if language in {'en', 'zh', 'fa'}:
        assert not re.search('[А-Яа-яЁё]', text)
    kb = callback.message.edit_text.await_args.kwargs['reply_markup']
    assert [(b.callback_data, b.url) for row in kb.inline_keyboard for b in row] == [
        ('admin_updates_check', None),
        ('admin_updates_info', None),
        (None, 'https://github.com/example/project/releases'),
        ('admin_panel', None),
    ]


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_release_description_preserves_author_text_and_escapes_markup(language):
    texts = get_texts(language)
    assert ui._release_description(SimpleNamespace(body='', short_description='Без описания'), texts) == texts.t(
        'ADMIN_UPDATES_NO_DESCRIPTION'
    )
    raw = 'Operator <example> & details'
    assert (
        ui._release_description(SimpleNamespace(body=raw, short_description=raw), texts)
        == 'Operator &lt;example&gt; &amp; details'
    )
    assert (
        ui._release_description(SimpleNamespace(body='Без описания', short_description='Без описания'), texts)
        == 'Без описания'
    )
