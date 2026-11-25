import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.bot import bot as bot_mod
from ai_support_bot.app.services import alerting, hub_events


class _FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Заглушка aiohttp.ClientSession: запоминает запросы, в сеть не ходит."""

    created = 0
    requests: list[dict] = []
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        type(self).created += 1
        self.timeout = kwargs.get('timeout')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        if type(self).error is not None:
            raise type(self).error
        type(self).requests.append({'url': url, 'json': json, 'headers': headers, 'timeout': self.timeout})
        return _FakeResponse()


@pytest.fixture
def fake_http(monkeypatch):
    _FakeSession.created = 0
    _FakeSession.requests = []
    _FakeSession.error = None
    monkeypatch.setattr(hub_events.aiohttp, 'ClientSession', _FakeSession)
    monkeypatch.setattr(hub_events.settings, 'HUB_URL', '')
    monkeypatch.setattr(hub_events.settings, 'HUB_TOKEN', '')
    return _FakeSession


def _enable(monkeypatch):
    monkeypatch.setattr(hub_events.settings, 'HUB_URL', 'https://hub.invalid/api/')
    monkeypatch.setattr(hub_events.settings, 'HUB_TOKEN', 'secret-token')


@pytest.mark.asyncio
async def test_without_hub_url_nothing_is_sent(fake_http):
    hub_events.emit('support.handoff', title='refund', text='запрос на возврат денег')
    await hub_events.drain()
    assert fake_http.created == 0
    assert fake_http.requests == []
    assert not hub_events._tasks


@pytest.mark.asyncio
async def test_with_hub_url_sends_expected_request(fake_http, monkeypatch):
    _enable(monkeypatch)
    hub_events.emit(
        'support.handoff', title='t' * 300, text='x' * 5000, severity='warn', quote='q' * 5000,
        data={'reason': 'refund'}, dedup_key='k1', silent=True,
    )
    assert fake_http.requests == []  # отправка идёт в фоне, emit не ждёт сети
    await hub_events.drain()

    assert len(fake_http.requests) == 1
    request = fake_http.requests[0]
    assert request['url'] == 'https://hub.invalid/api/events'
    assert request['headers'] == {'Authorization': 'Bearer secret-token'}
    assert request['timeout'].total == 3
    body = request['json']
    assert body == {
        'kind': 'support.handoff', 'severity': 'warn', 'title': 't' * 200, 'text': 'x' * 4000,
        'quote': 'q' * 4000, 'data': {'reason': 'refund'}, 'dedup_key': 'k1', 'silent': True,
    }
    assert 'source' not in body
    assert not hub_events._tasks


@pytest.mark.asyncio
async def test_defaults_omit_dedup_key(fake_http, monkeypatch):
    _enable(monkeypatch)
    hub_events.emit('support.error', title='сбой')
    await hub_events.drain()
    assert fake_http.requests[0]['json'] == {
        'kind': 'support.error', 'severity': 'info', 'title': 'сбой', 'text': '', 'quote': '',
        'data': {}, 'silent': False,
    }


@pytest.mark.asyncio
async def test_network_error_does_not_raise(fake_http, monkeypatch):
    _enable(monkeypatch)
    fake_http.error = hub_events.aiohttp.ClientConnectionError('boom')
    hub_events.emit('support.error', title='сбой')
    await hub_events.drain()
    assert fake_http.requests == []
    assert not hub_events._tasks


@pytest.mark.asyncio
async def test_slow_hub_is_cut_off_by_timeout(fake_http, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(hub_events, 'SEND_TIMEOUT', 0.05)

    async def never_answers(url, token, payload):
        await hub_events.asyncio.sleep(30)

    monkeypatch.setattr(hub_events, '_post', never_answers)
    hub_events.emit('support.error', title='сбой')
    await hub_events.asyncio.wait_for(hub_events.drain(), timeout=2)
    assert not hub_events._tasks


def test_emit_outside_event_loop_does_not_raise(fake_http, monkeypatch):
    _enable(monkeypatch)
    hub_events.emit('support.error', title='сбой')
    assert fake_http.created == 0


# --- точки подключения ---------------------------------------------------------------------


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, chat_id, action):
        return None


class _FakeMessage:
    def __init__(self, user_id, text):
        self.from_user = SimpleNamespace(id=user_id, full_name='Иван Клиентов', username='ivan_client')
        self.chat = SimpleNamespace(id=user_id)
        self.message_id = 1
        self.text = text
        self.caption = None
        self.photo = None
        self.bot = _FakeBot()
        self.answers = []

    async def answer(self, text, parse_mode=None):
        self.answers.append(text)


async def _run_handoff(monkeypatch, result, text='верните деньги'):
    async def fake_load():
        return None

    async def fake_generate_answer(db, telegram_id, question, image_url=None):
        return result

    monkeypatch.setattr(bot_mod.settings_store, 'load', fake_load)
    monkeypatch.setattr(bot_mod.settings_store, 'get_int', lambda key: 0)
    monkeypatch.setattr(bot_mod.settings_store, 'get_bool', lambda key: False)
    monkeypatch.setattr(bot_mod.support_agent, 'generate_answer', fake_generate_answer)
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '999')
    bot_mod._processed_updates.clear()
    message = _FakeMessage(555000111, text)
    await bot_mod.handle_message(message)
    bot_mod._processed_updates.clear()
    return message


@pytest.mark.asyncio
async def test_handoff_emits_reason_code(monkeypatch):
    calls = []
    monkeypatch.setattr(bot_mod.hub_events, 'emit', lambda *args, **kwargs: calls.append((args, kwargs)))
    await _run_handoff(monkeypatch, {
        'answer': 'Передаю обращение команде.', 'escalate': True, 'escalation_reason': 'refund',
        'conversation_id': 42, 'knowledge_used': 0, 'model': 'rules',
    }, text='верните деньги ' + 'я' * 600)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ('support.handoff',)
    assert kwargs['title'] == 'refund'
    assert kwargs['text'] == 'запрос на возврат денег'
    assert kwargs['severity'] == 'warn'
    assert kwargs['silent'] is True
    assert kwargs['quote'].startswith('верните деньги') and len(kwargs['quote']) == 500
    assert kwargs['data'] == {'reason': 'refund', 'conversation_id': 42, 'business': False}


@pytest.mark.asyncio
async def test_handoff_body_has_no_client_identity(fake_http, monkeypatch):
    _enable(monkeypatch)
    await _run_handoff(monkeypatch, {
        'answer': 'Секретный ответ бота.', 'escalate': True, 'knowledge_used': 0, 'model': 'gpt',
    })
    await hub_events.drain()

    assert len(fake_http.requests) == 1
    body = fake_http.requests[0]['json']
    assert body['title'] == 'unspecified'
    assert body['data'] == {'reason': 'unspecified', 'conversation_id': None, 'business': False}
    raw = json.dumps(body, ensure_ascii=False)
    for forbidden in ('555000111', 'telegram_id', 'Иван Клиентов', 'ivan_client', 'Секретный ответ бота'):
        assert forbidden not in raw


@pytest.mark.asyncio
async def test_answer_without_handoff_emits_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(bot_mod.hub_events, 'emit', lambda *args, **kwargs: calls.append((args, kwargs)))
    await _run_handoff(monkeypatch, {'answer': 'Готово.', 'escalate': False, 'knowledge_used': 1, 'model': 'gpt'})
    assert calls == []


@pytest.mark.asyncio
async def test_failure_alert_emits_error_without_client_data(monkeypatch):
    calls = []
    monkeypatch.setattr(alerting.hub_events, 'emit', lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(alerting.settings_store, 'get_bool', lambda key: True)
    monkeypatch.setattr(alerting.settings_store, 'get_int', lambda key: 900)
    monkeypatch.setattr(alerting.settings, 'ADMIN_IDS', '999')
    monkeypatch.setattr(alerting, '_bot_ref', _FakeBot())
    monkeypatch.setattr(alerting, '_last_sent', {})

    details = (
        'OperationalError: connection refused for user 555000111 (ivan@example.com)\n'
        '[SQL: SELECT * FROM users WHERE telegram_id = %s]\n[parameters: (555000111,)]'
    )
    await alerting.alert_admins('main_db_user_context', 'не читаются данные пользователя', details)
    await alerting.alert_admins('main_db_user_context', 'не читаются данные пользователя', details)

    assert len(calls) == 1  # повтор в окне тишины в журнал не уходит, как и админам
    args, kwargs = calls[0]
    assert args == ('support.error',)
    assert kwargs['title'] == 'не читаются данные пользователя'
    assert kwargs['severity'] == 'warn'
    assert kwargs['silent'] is True
    assert kwargs['text'].startswith('OperationalError: connection refused')
    for forbidden in ('555000111', 'ivan@example.com', 'SELECT', 'parameters'):
        assert forbidden not in kwargs['text']
