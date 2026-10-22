import json
from unittest.mock import AsyncMock, Mock

import pytest

from ai_support_bot.app.services import dialogue_tools, user_data
from ai_support_bot.app.services.openai_client import OpenAIClient


@pytest.mark.asyncio
async def test_intake_preserves_and_corrects_details_across_turns(monkeypatch):
    monkeypatch.setattr(dialogue_tools, 'build_user_context', AsyncMock(return_value='Данные своего аккаунта'))
    state = {}
    for args in (
        {'issue': 'payment_missing'},
        {'purchase_channel': 'site'},
        {'registration_method': 'email', 'email': 'customer@example.com'},
        {'receipt_reported': True},
    ):
        result = await dialogue_tools.execute_support_tool(
            'save_support_details', json.dumps(args), 42, state, 'Не пришли деньги, покупал на сайте, customer@example.com',
        )
        assert result['saved']
    assert state['email'] == 'customer@example.com'
    assert state['receipt_reported'] is True
    await dialogue_tools.execute_support_tool(
        'save_support_details', '{"purchase_channel":"telegram"}', 42, state, 'Ошибся, покупал в Telegram',
    )
    assert 'email' not in state
    assert 'registration_method' not in state


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{}', '{"email":"customer@example.com"}'])
async def test_refresh_uses_reported_site_email_and_bound_telegram(monkeypatch, arguments):
    refresh = AsyncMock(return_value='Свежие данные своего аккаунта')
    monkeypatch.setattr(dialogue_tools, 'build_user_context', refresh)
    state = {'purchase_channel': 'site', 'email': 'customer@example.com'}
    result = await dialogue_tools.execute_support_tool('refresh_account_data', arguments, 42, state, '')
    refresh.assert_awaited_once_with(42, email='customer@example.com')
    assert result['account_data'] == 'Свежие данные своего аккаунта'


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [
    '{"email":"guessed@example.com"}', '{"email":123}',
    '{"purchase_channel":"admin"}', '{"receipt_reported":"yes"}',
    '{"telegram_id":999}', 'null', '[]', 'not json',
])
async def test_invalid_or_hallucinated_details_do_not_change_state(arguments):
    state = {'issue': 'payment_missing'}
    result = await dialogue_tools.execute_support_tool('save_support_details', arguments, 42, state, '')
    assert 'error' in result
    assert state == {'issue': 'payment_missing'}


@pytest.mark.asyncio
async def test_email_lookup_never_queries_an_unbound_account(monkeypatch):
    result = Mock()
    result.mappings.return_value.first.return_value = None
    session = AsyncMock()
    session.execute.return_value = result
    monkeypatch.setattr(user_data, 'get_main_session', AsyncMock(return_value=session))
    remna = AsyncMock()
    monkeypatch.setattr(user_data, 'get_remnawave_stats', remna)

    context = await user_data.build_user_context(42, email='someone@example.com')

    sql, params = session.execute.call_args.args
    assert 'telegram_id = :tid' in str(sql)
    assert 'lower(email) = lower(:email)' in str(sql)
    assert params == {'tid': 42, 'email': 'someone@example.com'}
    assert 'не подтверждает и не опровергает' in context
    assert 'Баланс:' not in context
    remna.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_database_does_not_refresh_panel_for_unverified_email(monkeypatch):
    monkeypatch.setattr(user_data, 'get_main_session', AsyncMock(return_value=None))
    remna = AsyncMock()
    monkeypatch.setattr(user_data, 'get_remnawave_stats', remna)
    context = await user_data.build_user_context(42, email='someone@example.com')
    assert 'не подтверждены' in context
    remna.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_parses_null_content_and_preserves_tool_calls(monkeypatch):
    client = OpenAIClient()
    calls = [{'id': 'call_1', 'type': 'function', 'function': {'name': 'refresh_account_data', 'arguments': '{}'}}]
    post = AsyncMock(return_value={
        'choices': [{'message': {'content': None, 'tool_calls': calls}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
    })
    monkeypatch.setattr(client, '_post', post)
    result = await client.chat_completion([], 'gpt-4o-mini', 600, 0.2, tools=dialogue_tools.SUPPORT_TOOLS)
    assert result['content'] == ''
    assert result['tool_calls'] == calls
    payload = post.call_args.args[1]
    assert payload['tools'] == dialogue_tools.SUPPORT_TOOLS
    assert payload['parallel_tool_calls'] is False


@pytest.mark.asyncio
async def test_new_payment_case_discards_previous_email_and_receipt():
    state = {'issue': 'payment_missing', 'purchase_channel': 'site', 'email': 'old@example.com', 'receipt_reported': True}
    result = await dialogue_tools.execute_support_tool(
        'save_support_details', '{"new_case":true,"issue":"payment_missing"}', 42, state, '',
    )
    assert result['saved']
    assert state == {'issue': 'payment_missing'}


@pytest.mark.asyncio
async def test_invalid_refresh_does_not_access_database(monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr(dialogue_tools, 'build_user_context', refresh)
    result = await dialogue_tools.execute_support_tool('refresh_account_data', '{"email":"guessed@example.com"}', 42, {}, '')
    assert 'error' in result
    refresh.assert_not_awaited()


@pytest.mark.parametrize(('text', 'expected'), [
    ('Да, регистрировался через почту', 'email'),
    ('Регистрировалась по электронной почте', 'email'),
    ('Через почту', 'email'),
    ('Через Telegram', 'telegram'),
    ('Регистрировался другим способом', 'other'),
    ('Не через почту', 'other'),
    ('Я не регистрировался через почту', 'other'),
    ('Не знаю, регистрировался ли через почту', None),
    ('Как зарегистрироваться через почту?', None),
    ('Если зарегистрировался через почту?', None),
    ('Кажется, через почту', None),
    ('да', None),
])
def test_explicit_registration_details_do_not_depend_on_model_tool_call(text, expected):
    from types import SimpleNamespace

    history = [SimpleNamespace(role='assistant', content='На сайте регистрировались через почту или другим способом?')]
    assert dialogue_tools.extract_registration_method(text, history) == expected


@pytest.mark.parametrize(('text', 'expected'), [('да', 'email'), ('нет', 'other')])
def test_yes_no_registration_requires_last_binary_registration_question(text, expected):
    from types import SimpleNamespace

    history = [SimpleNamespace(role='assistant', content='На сайте вы регистрировались через почту?')]
    assert dialogue_tools.extract_registration_method(text, history) == expected
    history = [SimpleNamespace(role='assistant', content='Прислали чек?')]
    assert dialogue_tools.extract_registration_method(text, history) is None
