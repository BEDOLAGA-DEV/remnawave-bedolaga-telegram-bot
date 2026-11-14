import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import Message, Update


os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.bot import bot as bot_mod
from ai_support_bot.app.services import alerting
from ai_support_bot.app.services import openai_client as oc
from ai_support_bot.app.services.openai_client import OpenAIClient, OpenAIError
from ai_support_bot.app.services.telegram_html import sanitize_telegram_html


def _utf16(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


class LimitedBot:
    """Behaves like Telegram: rejects texts over 4096 characters and broken markup."""

    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str, str | None, dict]] = []
        self.photos: list[tuple[int, str, int]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id, text, parse_mode=None, **options):
        method = SendMessage(chat_id=chat_id, text=text[:10])
        if chat_id in self.fail_for:
            raise TelegramBadRequest(method=method, message='Bad Request: chat not found')
        if _utf16(text) > 4096:
            raise TelegramBadRequest(method=method, message='Bad Request: message is too long')
        self.sent.append((chat_id, text, parse_mode, options))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_photo(self, chat_id, photo, caption=None, reply_parameters=None):
        self.photos.append((chat_id, photo, reply_parameters.message_id))

    async def send_chat_action(self, chat_id, action, **kwargs):
        return True


class FakeMessage:
    def __init__(self, user_id: int, text: str, message_id: int = 1, bot=None) -> None:
        self.from_user = SimpleNamespace(id=user_id, full_name='Test User', username='testuser')
        self.chat = SimpleNamespace(id=user_id, type='private')
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.photo = None
        self.bot = bot or LimitedBot()
        self.answers: list[str] = []

    async def answer(self, text, parse_mode=None):
        self.answers.append(text)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    values = {'THROTTLE_SECONDS': 0, 'DAILY_MESSAGE_LIMIT': 0, 'ALERT_THROTTLE_SECONDS': 900}
    flags = {'ALERT_ADMINS_ON_FAILURE'}

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(bot_mod.settings_store, 'load', AsyncMock())
    monkeypatch.setattr(bot_mod.settings_store, 'get_int', lambda key: values.get(key, 0))
    monkeypatch.setattr(bot_mod.settings_store, 'get_bool', lambda key: key in flags)
    monkeypatch.setattr(bot_mod, 'AsyncSessionLocal', session)
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '999')
    monkeypatch.setattr(alerting, '_last_sent', {})
    monkeypatch.setattr(alerting, '_bot_ref', None)
    bot_mod._processed_updates.clear()
    yield
    bot_mod._processed_updates.clear()


def _agent(monkeypatch, result=None, error=None):
    async def generate(*args, **kwargs):
        if error is not None:
            raise error
        return result

    agent = AsyncMock(side_effect=generate)
    monkeypatch.setattr(bot_mod.support_agent, 'generate_answer', agent)
    return agent


# 1. Private chats only -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize('chat_type', ['group', 'supergroup'])
@pytest.mark.parametrize('text', ['Какой у меня баланс?', '/start'])
async def test_group_chat_is_ignored(monkeypatch, chat_type, text):
    monkeypatch.setattr(bot_mod, '_message_queue', bot_mod.MessageQueue(quiet_seconds=0, max_wait=0))
    agent = _agent(monkeypatch, {'answer': 'Баланс 100 ₽, ссылка https://connect.sligvpn.ru/secret', 'escalate': False})
    bot = Bot('123456:TEST_TOKEN')
    requests = []

    async def request(bot, method, **kwargs):
        requests.append(method)
        return True

    monkeypatch.setattr(bot.session, 'make_request', request)
    msg = Message(
        message_id=1, date=datetime.now(UTC), chat={'id': -100500, 'type': chat_type, 'title': 'Чат'},
        from_user={'id': 555, 'is_bot': False, 'first_name': 'Customer'}, text=text,
    )
    await bot_mod.build_dispatcher().feed_update(bot, Update(update_id=1, message=msg))
    agent.assert_not_called()
    assert requests == []


@pytest.mark.asyncio
async def test_queue_message_rejects_non_private_chat_even_without_dispatcher_filter(monkeypatch):
    agent = _agent(monkeypatch, {'answer': 'ok', 'escalate': False})
    message = FakeMessage(555, 'баланс')
    message.chat.type = 'group'
    await bot_mod.queue_message(message)
    agent.assert_not_called()
    assert message.answers == []


# 2. Alert on model failure ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_failure_alerts_admins_once_per_throttle_window(monkeypatch):
    alert_bot = LimitedBot()
    alerting.register_bot(alert_bot)
    secret = 'Incorrect API key provided: sk-proj-SECRET, see https://proxy.example/token123/v1'
    _agent(monkeypatch, error=OpenAIError(secret, status=429, code='insufficient_quota'))

    for message_id in (1, 2, 3):
        message = FakeMessage(555, 'мой email buyer@example.com, не работает', message_id=message_id)
        await bot_mod.handle_message(message)
        assert message.answers == ['⚠️ Не удалось получить ответ. Попробуйте позже.']

    assert [chat_id for chat_id, *_ in alert_bot.sent] == [999]
    text = alert_bot.sent[0][1]
    assert 'OpenAIError' in text and 'HTTP 429' in text and 'insufficient_quota' in text
    for leaked in ('sk-proj', 'SECRET', 'token123', 'proxy.example', 'buyer@example.com', 'не работает'):
        assert leaked not in text


@pytest.mark.asyncio
async def test_unexpected_error_alert_has_only_error_type(monkeypatch):
    alert_bot = LimitedBot()
    alerting.register_bot(alert_bot)
    _agent(monkeypatch, error=RuntimeError('connect postgres://admin:hunter2@db/prod failed'))
    message = FakeMessage(555, 'вопрос')
    await bot_mod.handle_message(message)
    assert message.answers == ['⚠️ Произошла ошибка. Попробуйте позже.']
    text = alert_bot.sent[0][1]
    assert 'RuntimeError' in text
    assert 'hunter2' not in text and 'postgres' not in text


@pytest.mark.asyncio
async def test_secretary_mode_failure_only_alerts_without_warning_to_customer(monkeypatch):
    alert = AsyncMock()
    monkeypatch.setattr(bot_mod.alerting, 'alert_admins', alert)
    _agent(monkeypatch, error=OpenAIError('Request timed out', code='timeout'))
    message = FakeMessage(555, 'Добрый день, VPN не работает')
    await bot_mod.handle_message(message, business_connection=SimpleNamespace(id='business-1', rights=None))
    assert message.answers == []
    alert.assert_awaited_once()
    key, title, details = alert.await_args.args
    assert key == 'generation:OpenAIError, код timeout'
    assert 'секретарь' in details
    assert 'VPN' not in details


# 3. Long escalations ---------------------------------------------------------------------------------------------

class _BalanceChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.ok = True

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        self.ok = self.ok and bool(self.stack) and self.stack.pop() == tag


def _balanced(chunk: str) -> bool:
    checker = _BalanceChecker()
    checker.feed(chunk)
    checker.close()
    return checker.ok and not checker.stack and not re.search(r'&(?!(?:amp|lt|gt|quot|#\d+);)', chunk)


@pytest.mark.asyncio
async def test_escalation_of_10000_chars_is_delivered_in_parts_within_limit(monkeypatch):
    question = '\n'.join(f'Сообщение {i}: <не работает> & 🛑 ' + 'подробности ' * 40 for i in range(20))
    assert len(question) > 10_000
    answer = '<b>Передаю обращение команде.</b> ' + (
        '<i>Проверьте <code>Happ</code> &amp; обновите подписку.</i>\n' * 40
    )
    _agent(monkeypatch, {
        'answer': answer, 'escalate': True, 'support_context': 'Контекст: ' + 'x' * 3000,
        'support_photo': 'photo-id',
    })
    message = FakeMessage(555, question)
    await bot_mod.handle_message(message)

    sent = message.bot.sent
    assert len(sent) >= 3
    assert all(chat_id == 999 for chat_id, *_ in sent)
    assert all(_utf16(text) <= 4096 for _, text, _, _ in sent)
    assert all(_balanced(text) for _, text, _, _ in sent)
    plain = bot_mod._html_to_plain(''.join(text for _, text, _, _ in sent))
    assert question.strip() in plain
    assert 'Внимание: Обращение требует внимания оператора' in sent[0][1]
    assert message.bot.photos == [(999, 'photo-id', 1)]


def test_html_split_keeps_tags_and_entities_whole():
    text = '<b>Заголовок</b>\n' + ''.join(
        f'<a href="https://web.slig.app/{i}">ссылка {i}</a> &amp; <i>курсив &lt;{i}&gt;</i> ' for i in range(400)
    )
    chunks = bot_mod._split_html(text, limit=500)
    assert len(chunks) > 10
    assert all(_utf16(chunk) <= 500 and _balanced(chunk) for chunk in chunks)
    assert bot_mod._html_to_plain(''.join(chunks)) == bot_mod._html_to_plain(text)


@pytest.mark.asyncio
async def test_broken_markup_falls_back_to_plain_text_parts():
    class StrictBot(LimitedBot):
        async def send_message(self, chat_id, text, parse_mode=None, **options):
            if parse_mode == 'HTML' and '<x' in text:
                raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text='x'),
                                         message="Bad Request: can't parse entities")
            return await super().send_message(chat_id, text, parse_mode, **options)

    bot = StrictBot()
    delivered = await bot_mod._notify_admins(bot, '<b>Обращение</b>\n<x>' + 'текст ' * 2000)
    assert delivered
    assert all(_utf16(text) <= 4096 for _, text, _, _ in bot.sent)
    assert any(parse_mode is None for _, _, parse_mode, _ in bot.sent)


@pytest.mark.asyncio
async def test_undelivered_handoff_alerts_and_does_not_pause_secretary_chat(monkeypatch):
    control = SimpleNamespace(id=7, owner_id=900, chat_id=555, revision=0)
    set_manual = AsyncMock()
    alert = AsyncMock()
    monkeypatch.setattr(bot_mod.business_control, 'get_by_id', AsyncMock(return_value=control))
    monkeypatch.setattr(bot_mod.business_control, 'active', AsyncMock(return_value=True))
    monkeypatch.setattr(bot_mod.business_control, 'set_manual', set_manual)
    monkeypatch.setattr(bot_mod.alerting, 'alert_admins', alert)
    _agent(monkeypatch, {'answer': 'Передаю оператору', 'escalate': True})
    message = FakeMessage(555, 'Позовите оператора', bot=LimitedBot(fail_for={900, 999}))

    token = bot_mod._business_turn.set((7, 0))
    try:
        await bot_mod.handle_message(message, business_connection=SimpleNamespace(id='business-1', rights=None))
    finally:
        bot_mod._business_turn.reset(token)

    assert message.answers == ['Передаю оператору']
    set_manual.assert_not_awaited()
    assert alert.await_args.args[0] == 'notification_delivery'
    assert 'chat not found' in alert.await_args.args[2]


# 4. Link whitelist -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('text', [
    '<a href="https://web.slig.app/balance">Баланс</a>',
    'Кабинет: https://web.slig.app/subscriptions',
    'Ваша ссылка: https://connect.sligvpn.ru/AbC123xyz_-Token9',
    '<a href="https://connect.sligvpn.ru/AbC123xyz_-Token9">Подключить</a>',
    'Скачайте Happ: https://happ.su/',
    'App Store: https://apps.apple.com/app/happ-proxy-utility/id6504287215',
    'Google Play: https://play.google.com/store/apps/details?id=com.happproxy',
    'Поддержка: https://t.me/SligVPNHelp, новости: https://t.me/sligvpn, бот: https://t.me/SligApp',
    'Откройте web.slig.app или happ.su',
    'Email: buyer@gmail.com, файл config.json, README.md, версия v2.0.1, т.е. всё верно',
])
def test_our_links_pass_unchanged(text):
    assert sanitize_telegram_html(text) == text


@pytest.mark.parametrize(('text', 'expected'), [
    ('<a href="https://evil.example/login">https://web.slig.app/balance</a>', 'https://web.slig.app/balance'),
    ('<a href="https://evil.example/login">Оплатить</a>', 'Оплатить'),
    ('<a href="https://evil.example/login">https://evil.example/login</a>', '[ссылка удалена]'),
    ('<a href="https://web.slig.app/">https://evil.example/</a>',
     '<a href="https://web.slig.app/">[ссылка удалена]</a>'),
    ('Оплатите тут: https://sligvpn-pay.example/login.', 'Оплатите тут: [ссылка удалена].'),
    ('Зайдите на sligvpn-pay.ru/login и оплатите', 'Зайдите на [ссылка удалена] и оплатите'),
    ('Сайт оплата.рф', 'Сайт [ссылка удалена]'),
    ('https://web.slig.app.evil.example/x', '[ссылка удалена]'),
    ('https://web.slig.app@evil.example/x', '[ссылка удалена]'),
    ('https://evil.example\\@web.slig.app/', '[ссылка удалена]'),
    ('Напишите https://t.me/SligVPN_support_bot', 'Напишите [ссылка удалена]'),
    ('<a href="https://t.me/joinchat/abc">Чат</a>', 'Чат'),
    ('<code>https://evil.example/x</code>', '[ссылка удалена]'),
])
def test_foreign_links_are_removed_or_masked(text, expected):
    assert sanitize_telegram_html(text) == expected


# 5. OpenAI timeouts and retries ----------------------------------------------------------------------------------

@pytest.fixture
def fake_time(monkeypatch):
    clock = {'now': 0.0}
    sleeps: list[float] = []

    async def sleep(delay):
        sleeps.append(delay)
        clock['now'] += delay

    monkeypatch.setattr(oc, '_monotonic', lambda: clock['now'])
    monkeypatch.setattr(oc.asyncio, 'sleep', sleep)
    return clock, sleeps


def _client(monkeypatch, clock, responses, duration=0.0):
    client = OpenAIClient()
    calls = []

    async def send(url, payload, timeout):
        calls.append((payload, timeout))
        spent = min(duration, timeout)
        clock['now'] += spent
        if duration > timeout:
            raise TimeoutError()
        response = responses[min(len(calls), len(responses)) - 1]
        return response if len(response) == 3 else (*response[:1], {}, response[1])

    monkeypatch.setattr(client, '_send', send)
    return client, calls


_OK = (200, {'choices': [{'message': {'content': 'Ответ'}}], 'usage': {}})


@pytest.mark.asyncio
@pytest.mark.parametrize(('status', 'body', 'code'), [
    (401, {'error': {'message': 'Incorrect API key provided: sk-...', 'code': 'invalid_api_key'}}, 'invalid_api_key'),
    (429, {'error': {'message': 'You exceeded your current quota', 'type': 'insufficient_quota',
                     'code': 'insufficient_quota'}}, 'insufficient_quota'),
    (403, {'error': 'Country not supported'}, None),
])
async def test_client_does_not_retry_permanent_errors(monkeypatch, fake_time, status, body, code):
    clock, sleeps = fake_time
    client, calls = _client(monkeypatch, clock, [(status, body), _OK])
    with pytest.raises(OpenAIError) as error:
        await client.chat_completion([], 'gpt-5.4-mini', 600, 0.2)
    assert len(calls) == 1
    assert sleeps == []
    assert (error.value.status, error.value.code) == (status, code)


@pytest.mark.asyncio
async def test_client_fits_all_attempts_into_deadline_without_final_sleep(monkeypatch, fake_time):
    clock, sleeps = fake_time
    client, calls = _client(monkeypatch, clock, [(503, None)], duration=20)
    with pytest.raises(OpenAIError):
        await client.chat_completion([], 'gpt-5.4-mini', 600, 0.2)
    assert clock['now'] <= oc.CHAT_DEADLINE_SECONDS
    assert len(sleeps) == len(calls) - 1
    assert all(timeout <= oc.CHAT_DEADLINE_SECONDS for _, timeout in calls)
    assert calls[0][0]['temperature'] == 0.2


@pytest.mark.asyncio
async def test_client_retries_server_errors_and_never_sleeps_after_last_attempt(monkeypatch, fake_time):
    clock, sleeps = fake_time
    client, calls = _client(monkeypatch, clock, [(500, {'error': {'message': 'boom'}})])
    with pytest.raises(OpenAIError, match='boom'):
        await client.chat_completion([], 'gpt-5.4-mini', 600, 0.2)
    assert len(calls) == 3
    assert sleeps == [1.5, 3.0]


@pytest.mark.asyncio
async def test_client_honours_retry_after_within_deadline(monkeypatch, fake_time):
    clock, sleeps = fake_time
    limited = (429, {'retry-after': '2'}, {'error': {'message': 'Rate limit', 'code': 'rate_limit_exceeded'}})
    client, calls = _client(monkeypatch, clock, [limited, _OK])
    result = await client.chat_completion([], 'gpt-5.4-mini', 600, 0.2)
    assert result['content'] == 'Ответ'
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_client_gives_up_when_retry_after_exceeds_deadline(monkeypatch, fake_time):
    clock, sleeps = fake_time
    limited = (429, {'retry-after': '120'}, {'error': {'message': 'Rate limit', 'code': 'rate_limit_exceeded'}})
    client, calls = _client(monkeypatch, clock, [limited, _OK])
    with pytest.raises(OpenAIError):
        await client.chat_completion([], 'gpt-5.4-mini', 600, 0.2)
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    (400, {'error': 'строка вместо объекта'}),
    (400, ['unexpected']),
    (200, ['unexpected']),
    (200, {'choices': 'нет'}),
])
async def test_client_turns_odd_bodies_into_openai_error(monkeypatch, fake_time, response):
    clock, _ = fake_time
    client, _ = _client(monkeypatch, clock, [response])
    with pytest.raises(OpenAIError):
        await client.chat_completion([], 'gpt-5.4-mini', 600, 0.2)
