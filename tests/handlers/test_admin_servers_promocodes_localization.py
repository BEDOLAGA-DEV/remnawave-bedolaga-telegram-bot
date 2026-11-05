"""Server and promo-code admin screens retain state and payloads across locales."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import promocodes, servers
from app.localization.texts import get_texts
from app.states import AdminStates


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


def _unwrap(handler):
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


def _server():
    return SimpleNamespace(
        id=7,
        squad_uuid='masked-1',
        display_name='Example <server>',
        original_name=None,
        is_available=True,
        price_kopeks=0,
        price_rubles=0,
        allowed_promo_groups=[],
        is_trial_eligible=False,
        country_code=None,
        max_users=0,
        current_users=4,
        description=None,
    )


@pytest.mark.parametrize('language', LANGUAGES)
def test_server_view_and_promo_keyboard_localize_preserving_payloads(language):
    text, keyboard = servers._build_server_edit_view(_server(), language)
    _, baseline = servers._build_server_edit_view(_server())
    assert 'Example &lt;server&gt;' in text
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [b.callback_data for row in baseline.inline_keyboard for b in row]
    promo = servers._build_server_promo_groups_keyboard(7, [{'id': 11, 'name': 'Example <group>'}], [11], language)
    assert promo.inline_keyboard[0][0].text == '✅ Example <group>'
    assert promo.inline_keyboard[0][0].callback_data == 'admin_server_promo_toggle_7_11'
    rendered = text + ''.join(b.text for b in buttons) + ''.join(b.text for row in promo.inline_keyboard for b in row)
    assert 'ADMIN_' not in rendered
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', rendered) is None, rendered


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_combo_promo_selection_keeps_type_state_and_localizes_message(language):
    callback = SimpleNamespace(
        data='promocode_type_combo', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    await _unwrap(promocodes.select_promocode_type)(callback, SimpleNamespace(language=language), state)
    state.update_data.assert_awaited_once_with(promocode_type='combo')
    state.set_state.assert_awaited_once_with(AdminStates.creating_promocode)
    call = callback.message.edit_text.await_args
    assert get_texts(language).t('ADMIN_PROMOCODE_UI_029') in call.args[0]
    assert call.kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'admin_promocodes'
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', call.args[0]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_invalid_promo_code_does_not_mutate_state_or_access_database(monkeypatch, language):
    message = SimpleNamespace(text='?', answer=AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock())
    lookup = AsyncMock()
    monkeypatch.setattr(promocodes, 'get_promocode_by_code', lookup)
    await _unwrap(promocodes.process_promocode_code)(message, SimpleNamespace(language=language), state, object())
    message.answer.assert_awaited_once_with(get_texts(language).t('ADMIN_PROMOCODE_UI_065'))
    state.update_data.assert_not_awaited()
    lookup.assert_not_awaited()


def test_server_helpers_receive_viewer_language_at_every_callsite():
    tree = ast.parse(Path(servers.__file__).read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {'_build_server_edit_view', '_build_server_promo_groups_keyboard'}
    ]
    assert len(calls) >= 3
    for call in calls:
        assert any(k.arg == 'language' and ast.unparse(k.value) == 'db_user.language' for k in call.keywords)
