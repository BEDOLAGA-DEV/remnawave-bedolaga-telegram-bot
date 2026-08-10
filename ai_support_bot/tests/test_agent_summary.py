import os

import pytest

os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')
os.environ.setdefault('AISUP_SUMMARY_EVERY_N_TURNS', '3')

from ai_support_bot.app.services.agent import _is_smalltalk
from ai_support_bot.app.services.knowledge_parser import _is_low_value


@pytest.mark.parametrize(
    'text',
    ['привет', 'здравствуйте', 'как дела?', 'спасибо', 'спасибо большое', 'спс',
     'ок', 'понял', 'до свидания', 'пока', 'благодарю вас', 'доброе утро'],
)
def test_smalltalk_positive(text):
    assert _is_smalltalk(text) is True


@pytest.mark.parametrize(
    'text',
    ['не работает vpn', 'как продлить подписку', 'хочу вернуть деньги', 'где моя ссылка',
     'не приходит оплата', 'как настроить на роутере', 'сколько стоит', 'баланс не пополнился'],
)
def test_smalltalk_negative(text):
    assert _is_smalltalk(text) is False


def test_low_value_keeps_real_support():
    assert _is_low_value('Как добавить устройство', 'Перейдите в бота, раздел Профиль, Мои подключения') is False
    assert _is_low_value('не работает vpn', 'Пришлите скриншот из приложения, так быстрее поможем') is False


def test_low_value_drops_partner_chatter():
    assert _is_low_value('обсудим реферальную систему инстаграм аудитория', 'давай процент 40 партнёр') is True
    assert _is_low_value('работаем бро', 'взаимно братан красавчик') is True


@pytest.mark.asyncio
async def test_rolling_summary_triggers_every_n_turns(monkeypatch, tmp_path):
    db_file = tmp_path / 'summary_test.db'
    monkeypatch.setenv('AISUP_DATABASE_URL', f'sqlite+aiosqlite:///{db_file}')

    from ai_support_bot.app.services import openai_client as oc
    from ai_support_bot.app.services import agent as agent_mod
    from ai_support_bot.app.services import summary_service as sum_mod

    summary_calls = {'n': 0}

    async def fake_chat_completion(messages, model, max_tokens, temperature):
        if 'сжимаешь диалог' in messages[0]['content']:
            summary_calls['n'] += 1
            return {'content': '• Проблема: тест', 'model': model, 'tokens_prompt': 1,
                    'tokens_completion': 1, 'tokens_cached': 0}
        return {'content': 'ответ', 'model': model, 'tokens_prompt': 1,
                'tokens_completion': 1, 'tokens_cached': 0}

    async def fake_retrieve(db, query):
        return []

    monkeypatch.setattr(oc.openai_client, 'chat_completion', fake_chat_completion)
    monkeypatch.setattr(agent_mod.openai_client, 'chat_completion', fake_chat_completion)
    monkeypatch.setattr(sum_mod.openai_client, 'chat_completion', fake_chat_completion)
    monkeypatch.setattr(agent_mod.rag_service, 'retrieve', fake_retrieve)

    from importlib import reload
    from ai_support_bot.app.core import config as config_mod
    config_mod.get_settings.cache_clear()
    from ai_support_bot.app.db import database as database_mod
    reload(database_mod)
    from ai_support_bot.app.db import crud
    from ai_support_bot.app.services import settings_store

    await database_mod.init_db()
    await settings_store.load()

    Session = database_mod.AsyncSessionLocal
    tid = 555
    for i in range(6):
        async with Session() as db:
            await agent_mod.support_agent.generate_answer(db, tid, f'вопрос {i} про подключение впн')

    assert summary_calls['n'] == 2

    async with Session() as db:
        conv = await crud.get_or_create_conversation(db, tid)
        assert conv.summary
        assert conv.user_turns_since_summary == 0
