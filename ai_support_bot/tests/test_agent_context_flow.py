import os

import pytest
import pytest_asyncio

os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.core.config import ESCALATION_MARKER


USER_CONTEXT = (
    'Telegram ID: 4242\n'
    'Баланс: 150.00 ₽\n'
    'Внимание: у пользователя 2 подписки.\n'
    'Подписки:\n'
    '  • подписка №1 (id=11), статус=active, до 01.10.2026, устройств=3\n'
    '  • подписка №2 (id=12), статус=active, до 05.11.2026, устройств=5'
)


@pytest_asyncio.fixture
async def flow(monkeypatch, tmp_path):
    db_file = tmp_path / 'flow.db'
    monkeypatch.setenv('AISUP_DATABASE_URL', f'sqlite+aiosqlite:///{db_file}')

    from importlib import reload

    from ai_support_bot.app.core import config as config_mod
    config_mod.get_settings.cache_clear()
    monkeypatch.setattr(config_mod, 'settings', config_mod.get_settings())

    from ai_support_bot.app.db import database as database_mod
    reload(database_mod)

    from ai_support_bot.app.services import agent as agent_mod
    from ai_support_bot.app.services import settings_store

    captured: dict = {'queries': [], 'prompts': [], 'reply': 'Готово.'}

    async def fake_chat_completion(messages, model, max_tokens, temperature):
        captured['prompts'].append(messages[0]['content'])
        captured['messages'] = messages
        return {
            'content': captured['reply'],
            'model': model,
            'tokens_prompt': 1,
            'tokens_completion': 1,
            'tokens_cached': 0,
        }

    async def fake_retrieve(db, query):
        captured['queries'].append(query)
        return captured.get('knowledge', [])

    async def fake_user_context(telegram_id):
        return USER_CONTEXT

    monkeypatch.setattr(agent_mod.openai_client, 'chat_completion', fake_chat_completion)
    monkeypatch.setattr(agent_mod.rag_service, 'retrieve', fake_retrieve)
    monkeypatch.setattr(agent_mod, 'build_user_context', fake_user_context)

    await database_mod.init_db()
    await settings_store.load()
    monkeypatch.setitem(settings_store._cache, 'SUMMARY_ENABLED', '0')

    yield captured, agent_mod, database_mod.AsyncSessionLocal


@pytest.mark.asyncio
async def test_user_data_is_always_injected_into_prompt(flow):
    captured, agent_mod, Session = flow

    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, 'какой у меня лимит устройств')

    prompt = captured['prompts'][-1]
    assert 'Данные текущего пользователя:' in prompt
    assert 'подписка №1 (id=11)' in prompt
    assert 'подписка №2 (id=12)' in prompt
    assert 'ПРАВИЛО ИСПОЛЬЗОВАНИЯ ДАННЫХ' in prompt


@pytest.mark.asyncio
async def test_topic_switch_keeps_focus_rule_and_contextualizes_retrieval(flow):
    captured, agent_mod, Session = flow

    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, 'не проходит оплата картой')
    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, 'сколько устройств можно подключить')
    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, 'а для второй подписки как?')

    last_query = captured['queries'][-1]
    assert last_query.strip().endswith('Текущий вопрос: а для второй подписки как?')
    assert 'сколько устройств можно подключить' in last_query

    prompt = captured['prompts'][-1]
    assert 'ПРАВИЛО ФОКУСА' in prompt

    history_roles = [m['role'] for m in captured['messages'][1:-1]]
    assert history_roles == ['user', 'assistant', 'user', 'assistant']


@pytest.mark.asyncio
async def test_marker_at_end_triggers_escalation_and_is_stripped(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = f'Уточняю этот вопрос, подождите. {ESCALATION_MARKER}'

    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'хочу вернуть деньги за подписку')

    assert result['escalate'] is True
    assert ESCALATION_MARKER not in result['answer']
    assert result['answer'] == 'Уточняю этот вопрос, подождите.'


@pytest.mark.asyncio
async def test_marker_in_the_middle_does_not_trigger_escalation(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = f'Напиши {ESCALATION_MARKER} в тексте — вот и всё, вопрос решён.'

    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'как настроить впн на роутере')

    assert result['escalate'] is False
    assert ESCALATION_MARKER not in result['answer']


@pytest.mark.asyncio
async def test_hedged_factual_answer_is_replaced_with_escalation_notice(flow):
    captured, agent_mod, Session = flow
    captured['knowledge'] = []
    captured['reply'] = 'Скорее всего подписка стоит около 250 рублей в месяц.'

    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'сколько стоит подписка на год')

    assert result['escalate'] is True
    assert result['hedged'] is True
    assert 'Скорее всего' not in result['answer']
    assert 'оператор' in result['answer'].lower()


@pytest.mark.asyncio
async def test_knowledge_backed_answer_is_not_escalated(flow):
    captured, agent_mod, Session = flow
    captured['knowledge'] = [{'score': 0.8, 'question': 'сколько стоит', 'answer': '250', 'content': 'Цена 250 ₽'}]
    captured['reply'] = 'Обычно подписка стоит 250 ₽ — так указано в тарифе.'

    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'сколько стоит подписка на месяц')

    assert result['escalate'] is False
    assert result['hedged'] is False
    assert result['knowledge_used'] == 1


@pytest.mark.asyncio
async def test_empty_knowledge_adds_no_guessing_instruction(flow):
    captured, agent_mod, Session = flow
    captured['knowledge'] = []

    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, 'есть ли у вас поддержка wireguard на apple tv')

    prompt = captured['prompts'][-1]
    assert 'База знаний не дала релевантных примеров' in prompt
    assert 'не гадай, эскалируй' in prompt


@pytest.mark.asyncio
async def test_followup_answer_is_not_served_from_cache(flow):
    captured, agent_mod, Session = flow
    question = 'не работает подключение на телефоне'

    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, question)
    calls_after_first = len(captured['prompts'])

    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, question)

    assert len(captured['prompts']) == calls_after_first + 1
