from argparse import Namespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from ai_support_bot import check_support
from ai_support_bot.app.db.models import Conversation


def test_live_checks_catch_empty_repeated_and_wrong_topic_answers():
    turn = check_support.Turn('Как подключить VPN?', forbidden=('Stars',))
    result = {'answer': '<b></b>', 'escalate': True}
    assert 'пустой пользовательский ответ' in check_support.checks(turn, result)
    text = 'Для подключения VPN перезапустите устройство и откройте приложение Incy. Проверьте свою подписку.'
    result['answer'] = text + '\n' + text
    assert 'ответ повторяется целиком' in check_support.checks(turn, result)
    result['answer'] = 'Пополните баланс через Stars'
    assert any('Stars' in issue for issue in check_support.checks(turn, result))


@pytest.mark.asyncio
async def test_live_check_reads_live_knowledge_but_stores_conversation_separately(monkeypatch, capsys):
    async with check_support.isolated_history() as live_session:
        live_engine = live_session.kw['bind']
        monkeypatch.setattr(check_support.database, 'AsyncSessionLocal', live_session)
        monkeypatch.setattr(check_support.database, 'engine', live_engine)
        monkeypatch.setattr(check_support.database, 'main_engine', None)
        monkeypatch.setattr(check_support.settings_store, 'load', AsyncMock())
        monkeypatch.setattr(check_support.settings_store, 'get', lambda key: 'test-model')

        async def retrieve(db, query):
            assert db.bind is live_engine
            return []

        async def generate(db, telegram_id, question):
            assert db.bind is not live_engine
            assert await check_support.rag_service.retrieve(db, question) == []
            db.add(Conversation(telegram_id=telegram_id))
            await db.commit()
            return {'answer': 'Передаю предложение команде.', 'escalate': True, 'knowledge_used': 0}

        monkeypatch.setattr(check_support.rag_service, 'retrieve', retrieve)
        monkeypatch.setattr(check_support.agent.support_agent, 'generate_answer', generate)
        await check_support.run(Namespace(telegram_id=42, email='qa@example.invalid', interactive=False, case='partnership'))
        async with live_session() as db:
            count = await db.scalar(select(func.count(Conversation.id)))
            assert count == 0
        assert 'Передаю предложение команде.' in capsys.readouterr().out


@pytest.mark.parametrize('answer', ['не обещай наличие кнопки', 'не предлагай вращать ещё раз', 'список приходит с сервера'])
def test_live_checks_detect_internal_instruction_leaks(answer):
    issues = check_support.checks(check_support.Turn('Где это на сайте?'), {'answer': answer, 'escalate': False})
    assert 'внутренняя инструкция попала в пользовательский ответ' in issues


def test_live_checks_detect_unclickable_code_url():
    result = {'answer': '<code>https://web.slig.app</code>', 'escalate': False}
    assert 'HTTPS-ссылка оформлена как код вместо кликабельной ссылки' in check_support.checks(check_support.Turn('Как продлить?'), result)
