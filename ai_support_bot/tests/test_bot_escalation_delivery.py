import os

import pytest

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.bot import bot as bot_mod


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    async def send_chat_action(self, chat_id, action):
        return None


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.full_name = 'Test User'
        self.username = 'testuser'


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, user_id: int, text: str, message_id: int = 1) -> None:
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(user_id)
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.photo = None
        self.bot = FakeBot()
        self.answers: list[str] = []

    async def answer(self, text, parse_mode=None):
        self.answers.append(text)


@pytest.fixture(autouse=True)
def _reset_bot_state():
    bot_mod._last_message_at.clear()
    bot_mod._throttle_warned.clear()
    bot_mod._processed_updates.clear()
    yield
    bot_mod._last_message_at.clear()
    bot_mod._throttle_warned.clear()
    bot_mod._processed_updates.clear()


def _patch_agent(monkeypatch, result: dict):
    async def fake_generate_answer(db, telegram_id, question, image_url=None):
        return result

    monkeypatch.setattr(bot_mod.support_agent, 'generate_answer', fake_generate_answer)


def _patch_settings(monkeypatch, values: dict):
    async def fake_load():
        return None

    monkeypatch.setattr(bot_mod.settings_store, 'load', fake_load)
    monkeypatch.setattr(bot_mod.settings_store, 'get_int', lambda key: values.get(key, 0))
    monkeypatch.setattr(bot_mod.settings_store, 'get_bool', lambda key: bool(values.get(key, False)))


@pytest.mark.asyncio
async def test_escalation_notice_goes_to_user_and_service_text_to_admins(monkeypatch):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 0, 'DAILY_MESSAGE_LIMIT': 0})
    _patch_agent(monkeypatch, {
        'answer': 'Уточняю этот вопрос у оператора, подождите, пожалуйста.',
        'escalate': True,
        'hedged': True,
        'knowledge_used': 0,
        'model': 'gpt-4o-mini',
    })
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '999,1000')

    message = FakeMessage(user_id=555, text='хочу вернуть деньги за подписку')
    await bot_mod.handle_message(message)

    assert message.answers == ['Уточняю этот вопрос у оператора, подождите, пожалуйста.']
    for _, text in message.bot.sent:
        assert 'Внимание: Обращение требует внимания оператора' in text
    assert {chat_id for chat_id, _ in message.bot.sent} == {999, 1000}

    for text in message.answers:
        assert 'Обращение требует внимания оператора' not in text
        assert 'ID: <code>' not in text


@pytest.mark.asyncio
async def test_admin_notification_is_never_sent_to_the_asking_user(monkeypatch):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 0, 'DAILY_MESSAGE_LIMIT': 0})
    _patch_agent(monkeypatch, {
        'answer': 'Передаю оператору.',
        'escalate': True,
        'hedged': False,
        'knowledge_used': 0,
        'model': 'gpt-4o-mini',
    })
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '555,999')

    message = FakeMessage(user_id=555, text='верните деньги пожалуйста')
    await bot_mod.handle_message(message)

    assert 555 not in {chat_id for chat_id, _ in message.bot.sent}
    assert {chat_id for chat_id, _ in message.bot.sent} == {999}
    assert message.answers == ['Передаю оператору.']


@pytest.mark.asyncio
async def test_non_escalated_answer_sends_nothing_to_admins(monkeypatch):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 0, 'DAILY_MESSAGE_LIMIT': 0})
    _patch_agent(monkeypatch, {
        'answer': 'Ваша подписка активна до 12.09.2026.',
        'escalate': False,
        'hedged': False,
        'knowledge_used': 2,
        'model': 'gpt-4o-mini',
    })
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '999')

    message = FakeMessage(user_id=777, text='до какого числа моя подписка')
    await bot_mod.handle_message(message)

    assert message.bot.sent == []
    assert message.answers == ['Ваша подписка активна до 12.09.2026.']


@pytest.mark.asyncio
async def test_throttle_blocks_flood_and_warns_once(monkeypatch):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 60, 'DAILY_MESSAGE_LIMIT': 0})
    calls = {'n': 0}

    async def fake_generate_answer(db, telegram_id, question, image_url=None):
        calls['n'] += 1
        return {'answer': 'ok', 'escalate': False, 'hedged': False, 'knowledge_used': 0, 'model': 'm'}

    monkeypatch.setattr(bot_mod.support_agent, 'generate_answer', fake_generate_answer)
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '')

    first = FakeMessage(user_id=42, text='не работает подключение впн', message_id=1)
    await bot_mod.handle_message(first)
    assert calls['n'] == 1

    warnings: list[str] = []
    for index in range(2, 12):
        flood = FakeMessage(user_id=42, text=f'ещё вопрос про впн {index}', message_id=index)
        await bot_mod.handle_message(flood)
        warnings.extend(flood.answers)

    assert calls['n'] == 1
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_duplicate_update_is_processed_once(monkeypatch):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 0, 'DAILY_MESSAGE_LIMIT': 0})
    calls = {'n': 0}

    async def fake_generate_answer(db, telegram_id, question, image_url=None):
        calls['n'] += 1
        return {'answer': 'ok', 'escalate': False, 'hedged': False, 'knowledge_used': 0, 'model': 'm'}

    monkeypatch.setattr(bot_mod.support_agent, 'generate_answer', fake_generate_answer)
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '')

    for _ in range(3):
        message = FakeMessage(user_id=91, text='не подключается впн на роутере', message_id=500)
        await bot_mod.handle_message(message)

    assert calls['n'] == 1


def test_admin_recipients_excludes_requesting_user(monkeypatch):
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '1,2,3')
    assert sorted(bot_mod._admin_recipients(exclude_telegram_id=2)) == [1, 3]
    assert sorted(bot_mod._admin_recipients()) == [1, 2, 3]
