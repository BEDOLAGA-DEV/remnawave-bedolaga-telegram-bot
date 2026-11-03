"""Campaign UI uses the viewer language without changing data or callback contracts."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import campaigns
from app.localization.texts import get_texts


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


def _unwrap(handler):
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


@pytest.mark.parametrize('language', LANGUAGES)
def test_campaign_summary_and_server_buttons_use_language_and_keep_values(language):
    texts = get_texts(language)
    campaign = SimpleNamespace(
        name='Example <campaign>',
        start_parameter='example&code',
        is_active=True,
        is_balance_bonus=False,
        is_subscription_bonus=True,
        subscription_traffic_gb=1536,
        subscription_device_limit=0,
        subscription_duration_days=7,
    )
    summary = campaigns._format_campaign_summary(campaign, texts)
    server = SimpleNamespace(id=7, squad_uuid='masked-1', is_available=True, display_name='Example <server>')
    keyboard = campaigns._build_campaign_servers_keyboard([server], ['masked-1'], language=language)
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [
        'campaign_toggle_server_7',
        'campaign_servers_save',
        'admin_campaigns',
    ]
    assert buttons[0].text == '✅ Example <server>'
    assert 'Example &lt;campaign&gt;' in summary
    assert 'example&amp;code' in summary
    assert '1.5' in summary and '∞' in summary
    assert 'ADMIN_CAMPAIGN_' not in summary
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', summary + ''.join(b.text for b in buttons)) is None


@pytest.mark.parametrize('language', LANGUAGES)
def test_traffic_keeps_original_threshold_and_precision(language):
    texts = get_texts(language)
    assert '12' in texts.format_traffic(12.3)
    assert '1.0' in texts.format_traffic(1024)
    assert '1.5' in texts.format_traffic(1536)
    assert '∞' in texts.format_traffic(0)
    assert texts.t('ADMIN_CAMPAIGN_UI_026').format(v0=12.345).count('12.3%') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_invalid_campaign_name_has_localized_error_and_no_state_mutation(language):
    message = SimpleNamespace(text='x', answer=AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    await _unwrap(campaigns.process_campaign_name)(message, SimpleNamespace(language=language), state, object())
    message.answer.assert_awaited_once_with(get_texts(language).t('ADMIN_CAMPAIGN_UI_073'))
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


def test_campaign_helpers_always_receive_viewer_language():
    source = Path(campaigns.__file__).read_text()
    tree = ast.parse(source)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in {'_build_campaign_servers_keyboard', '_ensure_subscription_campaign'}
    ]
    assert len(calls) == 12
    assert all(
        any(k.arg == 'language' and ast.unparse(k.value) == 'db_user.language' for k in c.keywords) for c in calls
    )


@pytest.mark.parametrize('language', LANGUAGES)
def test_campaign_tariff_name_is_escaped_without_translating_operator_content(language):
    campaign = SimpleNamespace(
        name='Example',
        start_parameter='example',
        is_active=True,
        is_balance_bonus=False,
        is_subscription_bonus=False,
        is_tariff_bonus=True,
        tariff=SimpleNamespace(name='<Example & plan>'),
        tariff_duration_days=7,
    )
    summary = campaigns._format_campaign_summary(campaign, get_texts(language))
    assert '&lt;Example &amp; plan&gt;' in summary
    assert '<Example' not in summary


@pytest.mark.asyncio
async def test_tariff_edit_prompt_escapes_operator_name(monkeypatch):
    campaign = SimpleNamespace(
        id=7, is_tariff_bonus=True, tariff_id=11, tariff=SimpleNamespace(name='<Example & plan>')
    )
    monkeypatch.setattr(campaigns, 'get_campaign_by_id', AsyncMock(return_value=campaign))
    monkeypatch.setattr(
        campaigns, 'get_all_tariffs', AsyncMock(return_value=[SimpleNamespace(id=11, name='<Example & plan>')])
    )
    callback = SimpleNamespace(
        data='admin_campaign_edit_tariff_7', message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    await _unwrap(campaigns.start_edit_campaign_tariff)(callback, SimpleNamespace(language='en'), object(), object())
    prompt = callback.message.edit_text.await_args.args[0]
    assert '&lt;Example &amp; plan&gt;' in prompt
    buttons = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert buttons[0][0].text == '✅ <Example & plan>'
    assert buttons[0][0].callback_data == 'campaign_edit_set_tariff_7_11'
