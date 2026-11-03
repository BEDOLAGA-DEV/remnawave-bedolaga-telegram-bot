"""Quick amount settings use account language without changing monetary values."""

import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import quick_amounts as ui
from app.localization.texts import get_texts


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_quick_amount_modes_and_callback_contract(language):
    config = SimpleNamespace(method_id='example', display_name='Operator label', quick_amounts=[10000, 12345])
    text = ui._view_text(config, {}, language)
    assert 'Operator label' in text
    assert '100 ₽, 123.45 ₽' in text
    for amounts in (None, [], [10000]):
        localized = ui._view_keyboard('example', amounts, language)
        baseline = ui._view_keyboard('example', amounts)
        assert [b.callback_data for row in localized.inline_keyboard for b in row] == [
            b.callback_data for row in baseline.inline_keyboard for b in row
        ]
        config.quick_amounts = amounts
        rendered = ui._view_text(config, {}, language)
        assert 'ADMIN_QUICK_' not in rendered
        if language in {'en', 'zh', 'fa'}:
            assert not re.search('[А-Яа-яЁё]', rendered)
            assert not re.search('[А-Яа-яЁё]', ' '.join(b.text for row in localized.inline_keyboard for b in row))


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
async def test_edit_prompt_uses_account_language(language):
    callback = SimpleNamespace(
        data='qamounts:edit:example',
        from_user=SimpleNamespace(language_code='ru'),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
    await inspect.unwrap(ui.start_edit_quick_amounts)(callback, state, db_user=SimpleNamespace(language=language))
    assert callback.message.edit_text.await_args.args[0] == get_texts(language).t('ADMIN_QUICK_PROMPT').format(
        limit=ui.MAX_QUICK_AMOUNTS
    )
    state.update_data.assert_awaited_once_with(quick_amounts_method_id='example')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_invalid_amount_preserves_limits_and_localizes_error(language):
    message = SimpleNamespace(text='-10', answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock())
    await inspect.unwrap(ui.process_quick_amounts)(message, state, db_user=SimpleNamespace(language=language))
    state.get_data.assert_not_awaited()
    message.answer.assert_awaited_once_with(
        get_texts(language)
        .t('ADMIN_QUICK_INVALID')
        .format(limit=ui.MAX_QUICK_AMOUNTS, maximum=ui.MAX_QUICK_AMOUNT_KOPEKS // 100)
    )
