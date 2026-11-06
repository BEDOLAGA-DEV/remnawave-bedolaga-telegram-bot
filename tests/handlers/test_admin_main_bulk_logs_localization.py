"""Admin summaries localize without translating logs or changing moderation payloads."""

import re
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import bulk_ban, main, system_logs
from app.localization.texts import get_texts


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


def _unwrap(handler):
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


def _assert_language(text, language):
    assert 'ADMIN_' not in text
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', text) is None, text


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_admin_panel_inserts_actual_online_statistics_for_every_locale(monkeypatch, language):
    from app.services.remnawave_service import RemnaWaveService

    stats = AsyncMock(return_value={'system': {'users_online': 123, 'users_last_day': 456, 'users_last_week': 789}})
    monkeypatch.setattr(RemnaWaveService, 'get_system_statistics', stats)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await _unwrap(main.show_admin_panel)(callback, SimpleNamespace(language=language), object())
    call = callback.message.edit_text.await_args
    for number in ['123', '456', '789']:
        assert number in call.args[0]
    _assert_language(call.args[0], language)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_bulk_ban_preserves_service_payload_and_result_precision(monkeypatch, language):
    parse = AsyncMock(return_value=[101, 102, 103])
    ban = AsyncMock(return_value=(2, 0, [103]))
    monkeypatch.setattr(bulk_ban.bulk_ban_service, 'parse_telegram_ids_from_text', parse)
    monkeypatch.setattr(bulk_ban.bulk_ban_service, 'ban_users_by_telegram_ids', ban)
    bot, db = object(), object()
    message = SimpleNamespace(text='101,102,103', bot=bot, answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    await _unwrap(bulk_ban.process_bulk_ban_list)(
        message, SimpleNamespace(id=7, full_name='Example', language=language), state, db
    )
    ban.assert_awaited_once_with(
        db=db,
        admin_user_id=7,
        telegram_ids=[101, 102, 103],
        reason='Массовая блокировка администратором',
        bot=bot,
        notify_admin=True,
        admin_name='Example',
    )
    call = message.answer.await_args
    assert '66.7%' in call.args[0]
    assert '<code>103</code>' in call.args[0]
    assert call.kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'admin_users'
    _assert_language(call.args[0], language)
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_invalid_bulk_ban_input_keeps_state_and_does_not_ban(monkeypatch, language):
    ban = AsyncMock()
    monkeypatch.setattr(bulk_ban.bulk_ban_service, 'ban_users_by_telegram_ids', ban)
    message = SimpleNamespace(text=None, answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    await _unwrap(bulk_ban.process_bulk_ban_list)(message, SimpleNamespace(language=language), state, object())
    ban.assert_not_awaited()
    state.clear.assert_not_awaited()
    _assert_language(message.answer.await_args.args[0], language)


@pytest.mark.parametrize('language', LANGUAGES)
def test_log_preview_preserves_raw_content_escaping_and_tail_limit(tmp_path, language):
    path = tmp_path / 'masked-1.log'
    missing = system_logs._build_logs_message(path, language)
    assert str(path) in missing
    _assert_language(missing, language)
    path.write_text('')
    empty = system_logs._build_logs_message(path, language)
    assert get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_001') in empty
    _assert_language(empty, language)
    content = 'a' * 2400 + '<b>Служебные данные & opaque</b>'
    path.write_text(content)
    result = system_logs._build_logs_message(path, language)
    preview = system_logs._format_preview_block(content[-system_logs.LOG_PREVIEW_LIMIT :])
    assert preview in result
    assert escape('<b>Служебные данные & opaque</b>') in result
    assert content[:2400] not in result
    _assert_language(result.replace(preview, ''), language)
    assert path.read_text() == content


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_log_refresh_uses_viewer_language_and_original_callbacks(monkeypatch, tmp_path, language):
    path = tmp_path / 'masked-1.log'
    path.write_text('opaque diagnostic')
    monkeypatch.setattr(system_logs, '_resolve_log_path', lambda: path)
    callback = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())
    await _unwrap(system_logs.refresh_system_logs)(callback, SimpleNamespace(language=language), object())
    call = callback.message.edit_text.await_args
    assert 'opaque diagnostic' in call.args[0]
    _assert_language(call.args[0], language)
    buttons = [button for row in call.kwargs['reply_markup'].inline_keyboard for button in row]
    assert [b.callback_data for b in buttons] == [
        'admin_system_logs_refresh',
        'admin_system_logs_download',
        'admin_submenu_system',
    ]
    _assert_language(' '.join(b.text for b in buttons), language)
    callback.answer.assert_awaited_once_with(get_texts(language).t('ADMIN_SYSTEM_LOGS_UI_010'))
