import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.services.response_rules import NOTICE, repeated_answer
from ai_support_bot.app.services.service_identity import official_service_reference, renewal_verification_answer, tariff_question


RENEWAL_UNKNOWN = renewal_verification_answer(None, {})[0]


@pytest_asyncio.fixture
async def flow(monkeypatch, tmp_path):
    monkeypatch.setenv('AISUP_DATABASE_URL', f'sqlite+aiosqlite:///{tmp_path / "flow.db"}')

    from importlib import reload

    from ai_support_bot.app.core import config as config_mod
    config_mod.get_settings.cache_clear()
    monkeypatch.setattr(config_mod, 'settings', config_mod.get_settings())

    from ai_support_bot.app.db import database as database_mod
    reload(database_mod)

    from ai_support_bot.app.services import agent as agent_mod
    from ai_support_bot.app.services import settings_store

    captured: dict = {'queries': [], 'prompts': [], 'reply': 'Готово.'}

    async def fake_chat_completion(messages, model, max_tokens, temperature, **kwargs):
        captured['prompts'].append(messages[0]['content'])
        captured['messages'] = messages
        result = {'content': captured['reply'], 'model': model, 'tokens_prompt': 1, 'tokens_completion': 1}
        if captured.get('responses'):
            result.update(captured['responses'].pop(0))
        return result

    async def fake_retrieve(db, query):
        captured['queries'].append(query)
        return []

    monkeypatch.setattr(agent_mod.openai_client, 'chat_completion', fake_chat_completion)
    monkeypatch.setattr(agent_mod.rag_service, 'retrieve', fake_retrieve)
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value='Telegram ID: 4242'))
    monkeypatch.setattr(agent_mod, '_response_cache', agent_mod._ResponseCache())
    monkeypatch.setattr(settings_store, 'AsyncSessionLocal', database_mod.AsyncSessionLocal)
    monkeypatch.setattr(settings_store, 'settings', config_mod.settings)
    monkeypatch.setattr(settings_store, '_cache', {})
    monkeypatch.setattr(settings_store, '_DEFAULTS', {})

    await database_mod.init_db()
    await settings_store.load()
    monkeypatch.setitem(settings_store._cache, 'SUMMARY_ENABLED', '0')
    try:
        yield captured, agent_mod, database_mod.AsyncSessionLocal
    finally:
        await database_mod.engine.dispose()


def _access(monkeypatch, result):
    from ai_support_bot.app.services import dialogue_tools
    read = AsyncMock(return_value=result)
    monkeypatch.setattr(dialogue_tools, 'read_account_support', read)
    return read


async def _seed(agent_mod, db, state, messages, age=None, summary=None):
    conversation = await agent_mod.crud.start_conversation(db, 4242)
    conversation.support_state = state
    conversation.summary = summary
    for role, content in messages:
        message = await agent_mod.crud.add_message(db, conversation.id, 4242, role, content)
        if age is not None:
            message.created_at = datetime.now(UTC) - age
    if age is not None:
        conversation.updated_at = datetime.now(UTC) - age
    await db.commit()
    return conversation.id


@pytest.mark.parametrize('question', [
    'Сколько стоит продлить подписку', 'сколько стоит продление', 'цена продления', 'сколько стоит месяц',
    'Сколько будет стоить продление на год?', 'Почём месяц?', 'А сколько тогда стоит продлить подписку?',
])
def test_price_questions_are_tariff_questions(question):
    assert tariff_question(question, [])


@pytest.mark.parametrize('question', [
    'Оплатил, сколько стоит продление?', 'Заплатила, а подписка не продлилась, сколько стоит месяц?',
    'Стоит ли переустановить Happ?', 'Сколько дней осталось?',
])
def test_payment_reports_and_other_questions_are_not_tariff_questions(question):
    assert not tariff_question(question, [])


@pytest.mark.asyncio
@pytest.mark.parametrize('question', [
    'Сколько стоит продлить подписку', 'сколько стоит продление', 'цена продления', 'сколько стоит месяц',
    'А сколько тогда стоит продлить подписку?',
])
async def test_price_question_clears_stale_renewal_flags_case_334(flow, monkeypatch, question):
    captured, agent_mod, Session = flow
    read = _access(monkeypatch, {'status': 'ok', 'active_paid_subscription': False})
    catalog = AsyncMock(return_value='Каталог: Стандарт, 5 устройств, месяц=219 ₽')
    monkeypatch.setattr(agent_mod.support_agent, '_build_catalog_block', catalog)
    async with Session() as db:
        await _seed(agent_mod, db, {'issue': 'payment_activation', 'renewal_verification': True}, [
            ('user', 'Оплатила 3 месяца, они добавятся к текущей подписке?'), ('assistant', RENEWAL_UNKNOWN),
        ])
    captured['reply'] = 'Продление Стандарта на месяц стоит 219 ₽.'
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, question)
        current = await agent_mod.crud.get_or_create_conversation(db, 4242)
        assert current.support_state == {'issue': 'other'}
    assert result['answer'].endswith('Продление Стандарта на месяц стоит 219 ₽.')
    assert 'Проверим оплату' not in result['answer']
    assert not result['escalate']
    assert 'месяц=219 ₽' in captured['prompts'][-1]
    catalog.assert_awaited_once()
    read.assert_not_awaited()


@pytest.mark.parametrize('reference', ['https://web.slig.app/', 'web.slig.app', 'https://slig.app'])
def test_own_site_is_recognized_as_payment_place(reference):
    assert official_service_reference('На сайте оплачивала ' + reference)['status'] == 'ours'


@pytest.mark.parametrize('reference', ['https://web.slig.app.evil.example/', 'support@slig.app', 'https://notslig.app'])
def test_similar_sites_are_not_official(reference):
    assert official_service_reference(reference) is None


def test_site_in_payment_report_after_price_question_is_not_tariff_question():
    history = [SimpleNamespace(role='user', content='Сколько стоит продлить подписку?')]
    assert tariff_question('На сайте https://web.slig.app', history)
    assert not tariff_question('Оплатила на https://web.slig.app, а дни не добавились', history)


@pytest.mark.asyncio
async def test_site_answer_moves_renewal_check_forward_case_324(flow, monkeypatch):
    captured, agent_mod, Session = flow
    _access(monkeypatch, {'status': 'ok', 'active_paid_subscription': True})
    async with Session() as db:
        await _seed(agent_mod, db, {'issue': 'payment_activation', 'renewal_verification': True}, [
            ('user', 'Оплатила 3 месяца, они добавятся к текущей подписке?'), ('assistant', RENEWAL_UNKNOWN),
        ])
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'На сайте оплачивала https://web.slig.app/')
    assert 'Проверим оплату' not in result['answer']
    assert 'чек этой операции' in result['answer']
    assert not result['escalate']


@pytest.mark.asyncio
async def test_same_renewal_template_is_not_sent_twice_case_324(flow, monkeypatch):
    _, agent_mod, Session = flow
    _access(monkeypatch, {'status': 'ok', 'active_paid_subscription': True})
    async with Session() as db:
        await _seed(agent_mod, db, {'issue': 'payment_activation', 'renewal_verification': True}, [
            ('user', 'Оплатила 3 месяца, они добавятся к текущей подписке?'), ('assistant', 'Здравствуйте!\n\n' + RENEWAL_UNKNOWN),
        ])
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'На сайте оплачивала')
    assert result['answer'] == NOTICE
    assert result['escalate']
    assert result['escalation_reason'] == 'repeated_answer'


@pytest_asyncio.fixture
async def main_db(monkeypatch, tmp_path):
    from ai_support_bot.app.services import account_support
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "main.db"}')
    Session = async_sessionmaker(engine)
    async with engine.begin() as db:
        await db.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, email TEXT)'))
        await db.execute(text('CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, '
                              'end_date TEXT, is_trial BOOLEAN, created_at TEXT, subscription_url TEXT)'))
        await db.execute(text("INSERT INTO users VALUES (1, 4242, 'owner@example.com')"))

    async def add(sub_id, status, trial, days, created_days_ago):
        async with engine.begin() as db:
            await db.execute(text('INSERT INTO subscriptions (id, user_id, status, end_date, is_trial, created_at) '
                                  'VALUES (:id, 1, :status, :end, :trial, :created)'), {
                'id': sub_id, 'status': status, 'trial': trial,
                'end': (datetime.now(UTC) + timedelta(days=days)).isoformat(),
                'created': (datetime.now(UTC) - timedelta(days=created_days_ago)).isoformat(),
            })

    async def session():
        return Session()
    monkeypatch.setattr(account_support, 'get_main_session', session)
    yield add
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(('subscriptions', 'active'), [
    ([('active', True, 3, 1), ('active', False, 30, 5)], True),
    ([('active', False, 30, 5), ('active', True, 3, 1)], True),
    ([('expired', True, -3, 1), ('active', False, 30, 5)], True),
    ([('active', True, 3, 1), ('expired', False, -1, 5)], False),
    ([('active', True, 3, 1)], False),
])
async def test_trial_next_to_paid_subscription_does_not_hide_paid_access_case_332(main_db, subscriptions, active):
    from ai_support_bot.app.services.account_support import read_account_support
    for index, subscription in enumerate(subscriptions, start=1):
        await main_db(index, *subscription)
    result = await read_account_support(4242, 'access')
    assert result['active_paid_subscription'] is active
    assert result['multiple_subscriptions'] is (len(subscriptions) > 1)


@pytest.mark.asyncio
async def test_paid_user_with_trial_gets_help_instead_of_receipt_request_case_332(flow, main_db):
    captured, agent_mod, Session = flow
    await main_db(1, 'active', True, 3, 1)
    await main_db(2, 'active', False, 30, 5)
    captured['reply'] = 'Подписка активна. Обновите подписку в Happ и выберите другой сервер.'
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'Оплатил, но VPN не работает')
    assert 'другой сервер' in result['answer']
    assert 'чек' not in result['answer']
    assert not result['escalate']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['unavailable', 'unverified'])
async def test_unconfirmed_access_is_handed_off_without_receipt_request(flow, monkeypatch, status):
    captured, agent_mod, Session = flow
    _access(monkeypatch, {'status': status})
    captured['reply'] = 'Пришлите чек, пожалуйста.'
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'Оплатила, включите впн')
    assert result['escalate']
    assert result['escalation_reason'] == 'payment_review'
    assert 'чек' not in result['answer']
    assert NOTICE in result['answer']


@pytest.mark.asyncio
async def test_receipt_request_is_not_repeated_for_any_new_message_dialogue_236(flow, monkeypatch):
    _, agent_mod, Session = flow
    _access(monkeypatch, {'status': 'ok', 'active_paid_subscription': False})
    async with Session() as db:
        first = await agent_mod.support_agent.generate_answer(db, 4242, 'Оплатила, включите впн')
    assert 'Пришлите чек этой операции' in first['answer']
    for question in ('Не работают', 'Вы тут?'):
        async with Session() as db:
            result = await agent_mod.support_agent.generate_answer(db, 4242, question)
        assert result['answer'] == NOTICE
        assert result['escalate']
        assert result['escalation_reason'] == 'repeated_answer'


@pytest.mark.asyncio
async def test_old_payment_flag_expires_before_new_problem_dialogue_236(flow, monkeypatch):
    captured, agent_mod, Session = flow
    read = _access(monkeypatch, {'status': 'ok', 'active_paid_subscription': False})
    async with Session() as db:
        old_id = await _seed(agent_mod, db, {'issue': 'payment_activation'}, [
            ('user', 'Оплатила, включите впн'), ('assistant', 'Проверяю доступ после оплаты. Пришлите чек.'),
        ], age=timedelta(days=6))
    captured['reply'] = 'Какое VPN-приложение вы используете?'
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'Не работают')
        current = await agent_mod.crud.get_or_create_conversation(db, 4242)
        assert current.id != old_id
        assert current.support_state == {}
    assert result['answer'] == 'Здравствуйте!\n\nКакое VPN-приложение вы используете?'
    assert not result['escalate']
    read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('age', 'expired'), [(timedelta(hours=25), True), (timedelta(hours=23), False)])
async def test_dialogue_state_lives_for_configured_hours(flow, age, expired):
    captured, agent_mod, Session = flow
    async with Session() as db:
        old_id = await _seed(agent_mod, db, {'issue': 'other'}, [
            ('user', 'VPN медленно работает на ноутбуке'), ('assistant', 'Выберите другой сервер.'),
        ], age=age, summary='OLD_SUMMARY')
    captured['reply'] = 'Выберите ближайший сервер в приложении.'
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'И на телефоне тоже медленно')
        current = await agent_mod.crud.get_or_create_conversation(db, 4242)
        assert (current.id != old_id) is expired
        assert current.summary == (None if expired else 'OLD_SUMMARY')
    assert ('OLD_SUMMARY' in captured['prompts'][-1]) is not expired
    assert any(m['content'] == 'VPN медленно работает на ноутбуке' for m in captured['messages']) is not expired
    assert result['answer'].startswith('Здравствуйте!') is expired


@pytest.mark.asyncio
async def test_expired_state_without_any_saved_messages_is_dropped(flow):
    captured, agent_mod, Session = flow
    async with Session() as db:
        old_id = await _seed(agent_mod, db, {'issue': 'payment_activation', 'renewal_verification': True}, [],
                             age=timedelta(days=3))
    captured['reply'] = 'Подключите VPN в приложении.'
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'Как подключить VPN на телевизоре?')
        current = await agent_mod.crud.get_or_create_conversation(db, 4242)
        assert current.id != old_id
    assert 'Проверим оплату' not in result['answer']


@pytest.mark.asyncio
async def test_zero_state_ttl_keeps_old_dialogue(flow):
    captured, agent_mod, Session = flow
    from ai_support_bot.app.services import settings_store
    await settings_store.set_value('STATE_TTL_HOURS', '0')
    async with Session() as db:
        old_id = await _seed(agent_mod, db, {'issue': 'other'}, [
            ('user', 'VPN медленно работает на ноутбуке'), ('assistant', 'Выберите другой сервер.'),
        ], age=timedelta(days=3))
    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, 'И на телефоне тоже медленно')
        current = await agent_mod.crud.get_or_create_conversation(db, 4242)
    assert current.id == old_id


@pytest.mark.asyncio
async def test_explicit_repeat_request_gets_the_same_template(flow, monkeypatch):
    _, agent_mod, Session = flow
    _access(monkeypatch, {'status': 'ok', 'active_paid_subscription': False})
    async with Session() as db:
        first = await agent_mod.support_agent.generate_answer(db, 4242, 'Оплатила, включите впн')
    async with Session() as db:
        result = await agent_mod.support_agent.generate_answer(db, 4242, 'Повторите, пожалуйста, что прислать')
    assert not result['escalate']
    assert 'Пришлите чек этой операции' in result['answer']
    assert result['answer'] in first['answer']


@pytest.mark.asyncio
async def test_repeated_greeting_and_model_answer_are_not_handed_off(flow):
    captured, agent_mod, Session = flow
    for _ in range(2):
        async with Session() as db:
            result = await agent_mod.support_agent.generate_answer(db, 4242, 'Привет')
        assert result['answer'] == 'Здравствуйте!'
        assert not result['escalate']
    captured['reply'] = 'Откройте настройки роутера, добавьте конфигурацию из личного кабинета и перезагрузите роутер.'
    for question in ('Как настроить VPN на роутере?', 'А на роутере Keenetic как настроить?'):
        async with Session() as db:
            result = await agent_mod.support_agent.generate_answer(db, 4242, question)
        assert not result['escalate']


def test_repeated_answer_ignores_greeting_prefix_and_markup():
    history = [SimpleNamespace(role='assistant', content='Здравствуйте!\n\n<b>Где вы оплачивали</b>: на сайте или в Telegram-боте?')]
    assert repeated_answer('Где вы оплачивали: на сайте или в Telegram-боте?', 'не помню', history)
    assert not repeated_answer('Где вы оплачивали: на сайте или в Telegram-боте?', 'ещё раз', history)
    assert not repeated_answer('Здравствуйте!', 'Привет', [SimpleNamespace(role='assistant', content='Здравствуйте!')])
    assert not repeated_answer('Пришлите чек.', 'да', [SimpleNamespace(role='user', content='Пришлите чек.')])


async def _add_messages(agent_mod, db, telegram_id, count, age, **options):
    conversation = await agent_mod.crud.start_conversation(db, telegram_id)
    for index in range(count):
        message = await agent_mod.crud.add_message(db, conversation.id, telegram_id, 'user' if index % 2 == 0 else 'assistant',
                                                   f'{telegram_id}-{index}', **options)
        message.created_at = datetime.now(UTC) - age + timedelta(seconds=index)
    await db.commit()
    return conversation


async def _contents(agent_mod, db, telegram_id):
    rows = await db.execute(select(agent_mod.crud.Message.content).where(agent_mod.crud.Message.telegram_id == telegram_id))
    return sorted(row[0] for row in rows.all())


@pytest.mark.asyncio
async def test_history_is_pruned_per_user(flow):
    _, agent_mod, Session = flow
    async with Session() as db:
        await _add_messages(agent_mod, db, 4242, 5, timedelta(days=2))
        await _add_messages(agent_mod, db, 5252, 3, timedelta(days=3))
        assert await agent_mod.crud.prune_messages(db, 4242, 2) == 3
        await db.commit()
        assert await _contents(agent_mod, db, 4242) == ['4242-3', '4242-4']
        assert await _contents(agent_mod, db, 5252) == ['5252-0', '5252-1', '5252-2']


@pytest.mark.asyncio
async def test_pruning_keeps_todays_messages_for_daily_limit(flow):
    _, agent_mod, Session = flow
    async with Session() as db:
        await _add_messages(agent_mod, db, 4242, 8, timedelta(minutes=5))
        before = await agent_mod.crud.count_user_messages_today(db, 4242)
        await agent_mod.crud.prune_messages(db, 4242, 2)
        await db.commit()
        assert before == 4
        assert await agent_mod.crud.count_user_messages_today(db, 4242) == 4


@pytest.mark.asyncio
async def test_pruning_keeps_photos_of_escalated_dialogue(flow):
    _, agent_mod, Session = flow
    async with Session() as db:
        conversation = await _add_messages(agent_mod, db, 4242, 1, timedelta(days=3), has_media=True,
                                           media_type='photo', media_file_id='receipt-photo')
        for index in range(3):
            message = await agent_mod.crud.add_message(db, conversation.id, 4242, 'user', f'later-{index}')
            message.created_at = datetime.now(UTC) - timedelta(days=2, seconds=-index)
        await db.commit()
        await agent_mod.crud.prune_messages(db, 4242, 1, conversation.id)
        await db.commit()
        assert await _contents(agent_mod, db, 4242) == ['4242-0', 'later-2']
        await agent_mod.crud.prune_messages(db, 4242, 1)
        await db.commit()
        assert await _contents(agent_mod, db, 4242) == ['later-2']


@pytest.mark.asyncio
async def test_answer_prunes_only_current_user_history(flow):
    _, agent_mod, Session = flow
    from ai_support_bot.app.services import settings_store
    await settings_store.set_value('HISTORY_LIMIT', '2')
    async with Session() as db:
        await _add_messages(agent_mod, db, 5252, 6, timedelta(days=2))
        await _add_messages(agent_mod, db, 4242, 4, timedelta(days=2))
    async with Session() as db:
        await agent_mod.support_agent.generate_answer(db, 4242, 'Привет')
        assert len(await _contents(agent_mod, db, 5252)) == 6
        assert len(await _contents(agent_mod, db, 4242)) == 2
