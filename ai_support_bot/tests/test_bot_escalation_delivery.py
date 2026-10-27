import os
from types import SimpleNamespace

import pytest

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.bot import bot as bot_mod


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str]] = []
        self.photo_replies: list[tuple[int, int]] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, chat_id, action):
        return None

    async def send_photo(self, chat_id, photo, caption=None, reply_parameters=None):
        self.photos.append((chat_id, photo))
        if reply_parameters:
            self.photo_replies.append((chat_id, reply_parameters.message_id))


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
    bot_mod._processed_updates.clear()
    yield
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
        'support_context': 'Указанный email: buyer@example.com\nТекст: <private>',
    })
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '999,1000')

    message = FakeMessage(user_id=555, text='хочу вернуть деньги за подписку')
    await bot_mod.handle_message(message)

    assert message.answers == ['Уточняю этот вопрос у оператора, подождите, пожалуйста.']
    for _, text in message.bot.sent:
        assert 'Внимание: Обращение требует внимания оператора' in text
        assert 'buyer@example.com' in text
        assert '&lt;private&gt;' in text
    assert {chat_id for chat_id, _ in message.bot.sent} == {999, 1000}

    for text in message.answers:
        assert 'Обращение требует внимания оператора' not in text
        assert 'ID: <code>' not in text


@pytest.mark.asyncio
async def test_yes_reaches_agent_instead_of_local_smalltalk_reply(monkeypatch):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 0, 'DAILY_MESSAGE_LIMIT': 0})
    _patch_agent(monkeypatch, {
        'answer': 'Напишите email аккаунта.', 'escalate': False, 'knowledge_used': 0,
        'model': 'gpt-5.4-mini',
    })
    message = FakeMessage(user_id=555, text='да')
    await bot_mod.handle_message(message)
    assert message.answers == ['Напишите email аккаунта.']


@pytest.mark.asyncio
async def test_saved_attachment_goes_only_to_admin_recipients(monkeypatch):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 0, 'DAILY_MESSAGE_LIMIT': 0})
    _patch_agent(monkeypatch, {
        'answer': 'Передаю обращение команде.', 'escalate': True, 'knowledge_used': 0,
        'model': 'rules', 'support_photo': 'telegram-photo-id',
    })
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '555,999')
    message = FakeMessage(user_id=555, text='Передайте <обращение> на проверку')
    await bot_mod.handle_message(message)
    assert message.bot.photos == [(999, 'telegram-photo-id')]
    assert message.bot.photo_replies == [(999, 1)]
    assert '&lt;обращение&gt;' in message.bot.sent[0][1]


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


@pytest.mark.asyncio
async def test_concurrent_photo_notifications_reply_to_matching_text(monkeypatch):
    import asyncio

    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '999')

    class YieldingBot(FakeBot):
        async def send_message(self, chat_id, text, parse_mode=None):
            result = await super().send_message(chat_id, text, parse_mode)
            await asyncio.sleep(0)
            return result

    bot = YieldingBot()
    await asyncio.gather(
        bot_mod._notify_admins(bot, 'Обращение A, ID 111', exclude_telegram_id=111, photo='photo-A'),
        bot_mod._notify_admins(bot, 'Обращение B, ID 222', exclude_telegram_id=222, photo='photo-B'),
    )
    for (_, photo), (_, reply_id) in zip(bot.photos, bot.photo_replies, strict=True):
        assert photo[-1] in bot.sent[reply_id - 1][1]
