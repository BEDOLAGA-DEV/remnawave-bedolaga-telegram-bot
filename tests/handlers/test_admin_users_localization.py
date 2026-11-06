from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import blocked_users, user_messages
from app.localization.texts import get_texts
from app.utils.validators import get_html_help_text, validate_html_tags


LANGUAGES = ['ru', 'en', 'ua', 'zh', 'fa']


@pytest.mark.parametrize('language', LANGUAGES)
def test_blocked_keyboard_localizes_without_changing_actions(language):
    keyboard = blocked_users.get_blocked_users_menu_keyboard({'blocked_count': 3}, language)
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ['admin_blocked_scan', 'admin_blocked_list', 'admin_users']
    assert buttons[0].text == get_texts(language).t('ADMIN_BLOCKED_BUTTON_START_SCAN')
    assert buttons[1].text == get_texts(language).t('ADMIN_BLOCKED_BUTTON_VIEW_BLOCKED').format(count=3)


@pytest.mark.parametrize('language', LANGUAGES)
async def test_messages_empty_list_uses_requested_language(monkeypatch, language):
    monkeypatch.setattr(user_messages, 'get_all_user_messages', AsyncMock(return_value=[]))
    message = SimpleNamespace(edit_text=AsyncMock())
    await user_messages._render_user_messages_list(message, AsyncMock(), language)
    call = message.edit_text.await_args
    assert call.args[0] == get_texts(language).t('ADMIN_USER_MESSAGE_017')
    assert call.kwargs['reply_markup'].inline_keyboard[0][0].text == get_texts(language).t('ADMIN_USER_MESSAGE_032')


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize(
    'source,key,values',
    [
        ('<unknown>x</unknown>', 'HTML_UNSUPPORTED_TAG', {'tag': 'unknown'}),
        ('</b>', 'HTML_CLOSING_WITHOUT_OPENING', {'tag': 'b'}),
        ('<b><i>x</b>', 'HTML_WRONG_NESTING', {'expected': 'i', 'actual': 'b'}),
        ('<b>x', 'HTML_UNCLOSED_TAG', {'tag': 'b'}),
    ],
)
def test_html_validation_keeps_decision_and_localizes_error(language, source, key, values):
    valid, error = validate_html_tags(source, language)
    assert not valid
    assert error == get_texts(language).t(key).format(**values)
    assert validate_html_tags('<b><i>x</i></b>', language) == (True, '')


@pytest.mark.parametrize('language', LANGUAGES)
def test_html_help_retains_literal_tag_examples(language):
    text = get_html_help_text(language)
    assert text == get_texts(language).t('HTML_HELP_TEXT')
    assert '&lt;tg-emoji emoji-id="123"&gt;😀&lt;/tg-emoji&gt;' in text
    assert '&lt;a href="url"&gt;' in text
    assert '<code>' in text


def test_html_validation_default_language_remains_russian():
    assert validate_html_tags('<unknown>') == (False, 'Неподдерживаемый тег: <unknown>')
    assert get_html_help_text() == get_html_help_text('ru')


@pytest.mark.parametrize('language', LANGUAGES)
async def test_admin_subscription_overview_uses_admin_language(monkeypatch, language):
    from app.handlers.admin import users

    user = SimpleNamespace(id=42, telegram_id=7001, email=None)
    service = SimpleNamespace(get_user_profile=AsyncMock(return_value={'user': user, 'subscription': None}))
    monkeypatch.setattr(users, 'UserService', lambda: service)
    monkeypatch.setattr(type(users.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(users, 'user_html_link', lambda user: 'A &amp; B')
    callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    assert await users._render_user_subscription_overview(callback, AsyncMock(), 42, language=language)
    call = callback.message.edit_text.await_args
    texts = get_texts(language)
    assert texts.t('ADMIN_USERS_PROFILE_000') in call.args[0]
    assert texts.t('ADMIN_USERS_PROFILE_012') in call.args[0]
    assert 'A &amp; B' in call.args[0]
    buttons = [b for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
    assert buttons[0].text == texts.t('ADMIN_USERS_PROFILE_055')
    assert buttons[0].callback_data == 'admin_sub_grant_trial_42'


def _unwrap(handler):
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


@pytest.mark.parametrize('language', LANGUAGES)
async def test_restriction_reason_is_stored_verbatim_and_escaped_for_display(monkeypatch, language):
    from app.handlers.admin import users

    user = SimpleNamespace(full_name='A < B', restriction_topup=True, restriction_subscription=False)
    monkeypatch.setattr(users, 'get_user_by_id', AsyncMock(return_value=user))
    state = SimpleNamespace(get_data=AsyncMock(return_value={'restriction_user_id': 42}), clear=AsyncMock())
    message = SimpleNamespace(text='  custom <b>reason</b> & test  ', answer=AsyncMock())
    db = AsyncMock()
    await _unwrap(users.save_restriction_reason)(message, SimpleNamespace(language=language), db, state)
    assert user.restriction_reason == 'custom <b>reason</b> & test'
    assert user.restriction_topup is True
    assert user.restriction_subscription is False
    db.commit.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert get_texts(language).t('ADMIN_USERS_RESTRICTIONS_011') in text
    assert 'custom &lt;b&gt;reason&lt;/b&gt; &amp; test' in text
    assert 'A &lt; B' in text


@pytest.mark.parametrize('amount,kopeks', [('25.5', 2550), ('-50', -5000)])
@pytest.mark.parametrize('language', LANGUAGES)
async def test_admin_balance_ui_preserves_signed_amount_and_ledger_description(monkeypatch, language, amount, kopeks):
    from app.handlers.admin import users

    service = SimpleNamespace(update_user_balance=AsyncMock(return_value=True))
    monkeypatch.setattr(users, 'UserService', lambda: service)
    state = SimpleNamespace(get_data=AsyncMock(return_value={'editing_user_id': 42}), clear=AsyncMock())
    message = SimpleNamespace(text=amount, answer=AsyncMock(), bot=object())
    db = AsyncMock()
    await _unwrap(users.process_balance_edit)(
        message, SimpleNamespace(language=language, id=7, full_name='Admin'), state, db
    )
    call = service.update_user_balance.await_args
    assert call.args[:3] == (db, 42, kopeks)
    assert call.args[4] == 7
    assert call.args[3] == ('Пополнение администратором: +25 ₽' if kopeks > 0 else 'Списание администратором: -50 ₽')
    action = get_texts(language).t('ADMIN_USERS_RESTRICTIONS_025' if kopeks > 0 else 'ADMIN_USERS_RESTRICTIONS_026')
    assert message.answer.await_args.args[0] == get_texts(language).t('ADMIN_USERS_RESTRICTIONS_035').format(
        value1=action, value2=users.settings.format_price(abs(kopeks))
    )
