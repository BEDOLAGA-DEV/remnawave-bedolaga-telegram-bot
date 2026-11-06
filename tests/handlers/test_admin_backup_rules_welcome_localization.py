"""Localized admin presentation preserves custom content and operation payloads."""

import html
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud.welcome_text import get_available_placeholders, replace_placeholders
from app.handlers.admin import backup, rules, welcome_text
from app.keyboards.admin import get_welcome_text_keyboard
from app.localization.texts import get_texts
from app.utils.display_mode import display_mode_label


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


def _unwrap(handler):
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


def _callback():
    return SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock())


def _buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def _assert_language(text, language):
    assert 'ADMIN_' not in text
    if language in {'en', 'zh', 'fa'}:
        assert re.search('[А-Яа-яЁё]', text) is None, text


@pytest.mark.parametrize('language', LANGUAGES)
def test_backup_reports_preserve_values_and_unknown_error_details(language):
    text = backup._localize_backup_message(
        '✅ Бекап успешно создан!\n📁 Файл: masked-1.sql.gz\n📊 Таблиц: 12\n📈 Записей: 1,234\n💾 Размер: 2.50 MB',
        language,
    )
    for value in ['masked-1.sql.gz', '12', '1,234', '2.50']:
        assert value in text
    _assert_language(text, language)
    detail = 'opaque provider detail <masked-1>'
    result = backup._localize_backup_message(f'❌ Ошибка восстановления: Ошибка psql: {detail}', language)
    assert result.endswith(detail)
    _assert_language(result, language)
    assert backup._localize_backup_message('Unrecognized report: opaque', language) == 'Unrecognized report: opaque'
    assert backup._localize_backup_message('❌ Файл бекапа не содержит данных', language) == get_texts(language).t(
        'ADMIN_BACKUP_RESULT_NO_DATA'
    )


@pytest.mark.parametrize('language', LANGUAGES)
def test_backup_and_real_common_welcome_keyboards_keep_callback_contract(language):
    filename = 'masked-1.sql.gz'
    keyboards = [
        (backup.get_backup_main_keyboard(language), backup.get_backup_main_keyboard()),
        (backup.get_backup_manage_keyboard(filename, language), backup.get_backup_manage_keyboard(filename)),
        (get_welcome_text_keyboard(language, True), get_welcome_text_keyboard('ru', True)),
        (get_welcome_text_keyboard(language, False), get_welcome_text_keyboard('ru', False)),
    ]
    for keyboard, baseline in keyboards:
        assert [b.callback_data for b in _buttons(keyboard)] == [b.callback_data for b in _buttons(baseline)]
        _assert_language(' '.join(b.text for b in _buttons(keyboard)), language)
    listing = backup.get_backup_list_keyboard([{'filename': filename, 'total_records': 1234}], language=language)
    assert listing.inline_keyboard[0][0].callback_data == f'backup_manage_{filename}'
    assert '1234' in listing.inline_keyboard[0][0].text
    _assert_language(' '.join(b.text for b in _buttons(listing)), language)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_rules_management_localizes_display_mode_and_buttons(language):
    callback = _callback()
    await _unwrap(rules.show_rules_management)(callback, SimpleNamespace(language=language), object())
    call = callback.message.edit_text.await_args
    buttons = _buttons(call.kwargs['reply_markup'])
    assert display_mode_label(rules.settings.SERVICE_RULES_DISPLAY_MODE, language) in buttons[3].text
    assert buttons[3].callback_data == 'admin_rules_display_mode'
    _assert_language(call.args[0] + ' '.join(b.text for b in buttons), language)


@pytest.mark.parametrize('language', LANGUAGES)
def test_welcome_validation_keeps_original_acceptance_rules(language):
    for text in ['<b>Hello {user_name}</b>', '<a href="tg://resolve?domain=example">Link</a>', '<i>ok</i>']:
        assert welcome_text.validate_html_tags(text, language)[0]
    for text in [
        '<div>Hello</div>',
        '<b>unclosed',
        '</b>',
        '<b></i>',
        '<a href="ftp://example">Link</a>',
        '<a>Link</a>',
    ]:
        valid, error = welcome_text.validate_html_tags(text, language)
        assert not valid
        _assert_language(error, language)
    _assert_language(welcome_text.get_telegram_formatting_info(language), language)
    descriptions = get_available_placeholders(language)
    assert set(descriptions) == {'{user_name}', '{first_name}', '{username}', '{username_clean}'}
    _assert_language(descriptions['{user_name}'], language)
    _assert_language(' '.join(descriptions.values()).replace('друг', ''), language)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_saving_welcome_preserves_operator_html_and_escapes_only_admin_preview(monkeypatch, language):
    custom = '<b>Example &amp; {user_name}</b>'
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(welcome_text, 'set_welcome_text', save)
    monkeypatch.setattr(welcome_text, 'get_current_welcome_text_settings', AsyncMock(return_value={'is_enabled': True}))
    message = SimpleNamespace(text=custom, answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    db = object()
    await _unwrap(welcome_text.process_welcome_text_edit)(message, state, SimpleNamespace(id=7, language=language), db)
    save.assert_awaited_once_with(db, custom, 7)
    call = message.answer.await_args
    assert f'<code>{html.escape(custom)}</code>' in call.args[0]
    assert custom not in call.args[0]
    assert get_texts(language).t('ADMIN_WELCOME_UI_002') in call.args[0]
    _assert_language(call.args[0], language)
    state.clear.assert_awaited_once()


def test_custom_welcome_substitution_retains_existing_fallback_and_escaping():
    assert replace_placeholders('{user_name}', SimpleNamespace()) == 'друг'
    assert replace_placeholders('{user_name}', SimpleNamespace(first_name='<Example>')) == '&lt;Example&gt;'


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_backup_create_localizes_service_report_without_changing_operation(monkeypatch, language):
    report = '✅ Бекап успешно создан!\n📁 Файл: masked-1.sql.gz\n📊 Таблиц: 12\n📈 Записей: 1,234\n💾 Размер: 2.50 MB'
    create = AsyncMock(return_value=(True, report, 'masked-1.sql.gz'))
    monkeypatch.setattr(backup.backup_service, 'create_backup', create)
    progress = SimpleNamespace(edit_text=AsyncMock())
    callback = _callback()
    callback.message.edit_text.return_value = progress
    await _unwrap(backup.create_backup_handler)(
        callback, SimpleNamespace(id=7, telegram_id=None, email=None, language=language), object()
    )
    create.assert_awaited_once_with(created_by='#7', compress=True)
    result = progress.edit_text.await_args.args[0]
    assert 'masked-1.sql.gz' in result
    assert '1,234' in result
    assert '2.50' in result
    _assert_language(result, language)


@pytest.mark.parametrize(
    'language, expected', [('ru', 'друг'), ('en', 'friend'), ('ua', 'друже'), ('zh', '朋友'), ('fa', 'دوست')]
)
def test_welcome_missing_name_uses_recipient_language(language, expected):
    user = SimpleNamespace(language=language, first_name='  ', username=None)
    assert replace_placeholders('{user_name}|{first_name}|{username}|{username_clean}', user) == '|'.join(
        [expected] * 4
    )
    user.first_name = '<Example>'
    assert replace_placeholders('{user_name}', user) == '&lt;Example&gt;'
