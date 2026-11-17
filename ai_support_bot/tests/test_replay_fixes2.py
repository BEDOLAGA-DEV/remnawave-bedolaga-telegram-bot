"""Fixes found by replaying 100 real tickets (2026-11-15): the phrases are the customers' own."""
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.core.config import ESCALATION_MARKER
from ai_support_bot.app.services.account_support import moscow_time, wheel_reward_answer
from ai_support_bot.app.services.answer_buttons import mentions_connect
from ai_support_bot.app.services.response_rules import (
    NOTICE,
    drop_scope_disclaimer,
    initial_greeting,
    mandatory_handoff,
    money_dispute_topic,
    neutral_self_reference,
)
from ai_support_bot.app.services.emoji_style import decorate


USER_CONTEXT = (
    'Telegram ID: 4242\n'
    'Баланс: 0.00 ₽\n'
    'Подписки:\n'
    '  • подписка №1 (id=11), статус=active, триал=нет, до 01.12.2026, устройств=5'
)
TEMPLATE = ('🛡 Попробуйте по порядку:\n- сменить сервер в приложении;\n- обновить подписку в приложении;\n'
            '- проверить Wi‑Fi и мобильный интернет.')


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
        result = {'content': captured['reply'], 'model': model, 'tokens_prompt': 1, 'tokens_completion': 1}
        if captured.get('responses'):
            result.update(captured['responses'].pop(0))
        return result

    async def fake_retrieve(db, query):
        captured['queries'].append(query)
        return []

    monkeypatch.setattr(agent_mod.openai_client, 'chat_completion', fake_chat_completion)
    monkeypatch.setattr(agent_mod.rag_service, 'retrieve', fake_retrieve)
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=USER_CONTEXT))
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


async def _ask(agent_mod, Session, question):
    async with Session() as db:
        return await agent_mod.support_agent.generate_answer(db, 4242, question)


def _labels(result):
    return [button['text'] for row in result['buttons'] for button in row]


# 1. Money and account go to the team at once.

@pytest.mark.parametrize(('question', 'reason'), [
    # #281
    ('можно ли как то отменить подписку и вернуть ту часть денег, которая не будет использоваться'
     '(если возможность отменить подписку есть)', 'subscription_cancellation'),
    ('Можно ли вернуть ту часть денег, которая не будет использоваться?', 'refund'),
    # #258
    ('Здравствуйте, есть ли возможность отменить подписку? Я случайно купил семейный тариф, а мне нужен стандартный', 'wrong_tariff'),
    ('Есть ли возможность отменить подписку?', 'subscription_cancellation'),
    # #172
    ('Оплатила дважды подписку,деньги списались, пополнений нет ', 'payment_dispute'),
    ('Деньги списали два раза за одну подписку', 'payment_dispute'),
    # #294
    ('Недавно взломали аккаунт, пришлось сбросить. В связи с этим изменился и ID аккаунта, почему прошу перенести подписку. '
     'Вот ссылка на мою подписку: https://connect.sligvpn.ru/GMYwvdkHgW9_A01f', 'account_transfer'),
    # #279
    ('У меня проблема с тг аккаунтом @KOXSIK перенесите впн на этот аккаунт', 'account_transfer'),
    ('телефон просто разбил и тг потерял к сожалению', 'account_transfer'),
    # #293
    ('Не отменяется пробная подписка - списывает деньги каждый день ', 'subscription_cancellation'),
    # #280
    ('мне на почту пришло уведомление, что с меня хотят снять деньги за подписку. но подписки никогда не было', 'unrecognized_charge'),
    # #233, #176, #262
    ('Отмена подписки на 30 дней, я такую подписку не оформляла ! Откуда она ?!', 'subscription_cancellation'),
    ('Я не пользуюсь вашим впн а оплата списывается каждый месяц автоматически с карты ', 'subscription_cancellation'),
    ('Отмена вашей услуги', 'subscription_cancellation'),
])
def test_money_and_account_requests_are_handed_over(question, reason):
    assert mandatory_handoff(question, []) == reason
    assert money_dispute_topic(question)


@pytest.mark.parametrize('question', [
    'Как отключить автопродление?',
    'Как отменить авто списание за подписку?',
    'Здравствуйте, хотел бы отключить эту функцию, помогите мне пожалуйста!!!',
    'Не могу отключить автопродление',
    'Хочу продлить подписку',
    'Сколько стоит подписка?',
    'Как оплатить?',
    'Как вернуть ссылку подписки?',
    'Верно ли, что деньги спишут автоматически?',
    # #245: a new phone is a device question
    'Надо со старого телефона перенести в happ подписку на новый телефон, а в приложении пишет что максимальное число устройств',
    # #318: two top-ups are not a double charge
    'Здравствуйте, дважды пополнил баланс и продлил подписку до 13 января, но бот всё равно пишет, что подписка истекла',
])
def test_self_service_money_questions_are_not_forced_to_the_team(question):
    assert mandatory_handoff(question, []) is None


@pytest.mark.parametrize('question', ['Хочу продлить подписку', 'Сколько стоит подписка?', 'Как оплатить?', 'VPN не работает'])
def test_ordinary_questions_are_not_a_money_dispute(question):
    assert not money_dispute_topic(question)


@pytest.mark.asyncio
async def test_wrong_tariff_258_is_handed_over_plain_without_renew_button(flow):
    captured, agent_mod, Session = flow
    result = await _ask(agent_mod, Session, 'Здравствуйте, есть ли возможность отменить подписку? '
                                            'Я случайно купил семейный тариф, а мне нужен стандартный')
    assert result['escalate']
    assert result['escalation_reason'] == 'wrong_tariff'
    assert result['buttons'] == []
    assert 'tg-emoji' not in result['answer'] and '🎧' not in result['answer']
    assert result['answer'] == 'Здравствуйте!\n\n' + NOTICE
    assert not captured['prompts']


@pytest.mark.asyncio
@pytest.mark.parametrize(('question', 'reason'), [
    ('можно ли как то отменить подписку и вернуть ту часть денег, которая не будет использоваться', 'subscription_cancellation'),
    ('Оплатила дважды подписку,деньги списались, пополнений нет ', 'payment_dispute'),
    ('Недавно взломали аккаунт, пришлось сбросить. Прошу перенести подписку на новый аккаунт', 'account_transfer'),
    ('Не отменяется пробная подписка - списывает деньги каждый день ', 'subscription_cancellation'),
])
async def test_money_handoffs_skip_the_model_receipt_and_buttons(flow, question, reason):
    captured, agent_mod, Session = flow
    captured['reply'] = 'Пришлите чек.\n[[BUTTONS: renew]]'
    result = await _ask(agent_mod, Session, question)
    assert result['escalate']
    assert result['escalation_reason'] == reason
    assert result['buttons'] == []
    assert 'tg-emoji' not in result['answer']
    assert 'чек' not in result['answer']
    assert not captured['prompts']


@pytest.mark.asyncio
async def test_model_handoff_without_reason_on_a_charge_is_plain_and_greets_once(flow):
    """«Здравствуйте! / 🎧 Здравствуйте. Это похоже на спорное списание… [КОМАНДЕ:None]» from the replay."""
    captured, agent_mod, Session = flow
    captured['reply'] = ('Здравствуйте. Это похоже на спорное списание, такое обращение передаю на проверку.\n'
                         '[[BUTTONS: renew]]\n' + ESCALATION_MARKER)
    result = await _ask(agent_mod, Session, 'Пришло уведомление о списании за подписку, что это?')
    assert captured['prompts']
    assert result['escalate']
    assert result['escalation_reason'] is None
    assert result['answer'] == 'Здравствуйте. Это похоже на спорное списание, такое обращение передаю на проверку.'
    assert result['answer'].count('Здравствуйте') == 1
    assert result['buttons'] == []


@pytest.mark.asyncio
async def test_autopay_question_never_gets_a_renew_button(flow):
    captured, agent_mod, Session = flow
    from ai_support_bot.app.services import settings_store
    settings_store._cache['NAVIGATION_ENABLED'] = '0'
    captured['reply'] = 'Отключить автопродление можно в настройках подписки.\n[[BUTTONS: renew, subscription]]'
    result = await _ask(agent_mod, Session, 'Как отменить автопродление тарифа?')
    assert captured['prompts']
    assert not result['escalate']
    assert 'Продлить подписку' not in _labels(result)
    assert 'Купить подписку' not in _labels(result)
    assert _labels(result) == ['Моя подписка']


@pytest.mark.asyncio
async def test_cancel_renewal_wish_is_not_mistaken_for_a_renewal(flow):
    captured, agent_mod, Session = flow
    result = await _ask(agent_mod, Session, 'Хочу отменить продление подписки')
    assert result['escalate']
    assert result['escalation_reason'] == 'subscription_cancellation'
    assert result['buttons'] == []
    assert 'кнопке' not in result['answer']


# 2. «Already tried» goes to diagnostics instead of the same template.

@pytest.mark.parametrize('question', [
    # #303, #302, #301, #298, #297, #304
    'впн работает только с wi-fi. как только переключаешься на сотовые данные, все сервера недоступны. проблема длится уже около недели.',
    'Добрый день!Уже 3 день,у меня переодически не работает впн надолго.Можно ли решить эту проблему?',
    'Нет впн,тест пинга нажимаю все равно всё n\\a и так 3 сутки',
    'Пробовал через разные приложения. INCY, TrueSlasn, Happ. Везде одинаково не работает. Сеть МТС.',
    'Неделю уже вот такая ситуация, обновляю, но ничего не меняется',
    'Послание пару дней ужасно работает ВПН, много серверов вообще не работают ,а остальные с большим пингом',
    'Уже всё перепробовала, VPN не подключается',
    'Давно не работает VPN',
    'Несколько дней не грузит ничего через VPN',
])
def test_already_tried_or_long_outage_goes_to_diagnostics(question):
    assert mandatory_handoff(question, []) == 'diagnostics_failed'


@pytest.mark.parametrize('question', [
    'VPN не работает', 'Не работает', 'Купил подписку на неделю, не работает', 'Купил 3 дня назад, не работает',
    'Как продлить подписку на неделю?', 'Оплатила неделю назад, подписка не работает',
    # the App Store case keeps its region instructions
    'Happ недоступен в App Store, уже пробовал искать',
])
def test_first_complaint_still_gets_basic_steps(question):
    assert mandatory_handoff(question, []) is None


@pytest.mark.parametrize('question', ['уже пробовали', 'да не помогло', 'Так он не работает', 'Ничего не поменялось', 'Та же ошибка'])
def test_failure_after_the_template_is_handed_over(question):
    history = [SimpleNamespace(role='user', content='тикток не открывается'), SimpleNamespace(role='assistant', content=TEMPLATE)]
    assert mandatory_handoff(question, history) == 'diagnostics_failed'


@pytest.mark.parametrize('question', ['А как обновить подписку?', 'Спасибо, заработало', 'А если не поможет?', 'Какой сервер выбрать?'])
def test_questions_about_the_template_are_not_a_failure(question):
    history = [SimpleNamespace(role='user', content='тикток не открывается'), SimpleNamespace(role='assistant', content=TEMPLATE)]
    assert mandatory_handoff(question, history) is None


@pytest.mark.asyncio
async def test_template_is_not_repeated_after_already_tried_242(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = TEMPLATE
    first = await _ask(agent_mod, Session, 'тикток открывается старый,раньше впн работал очень хорошо,сейчас нет')
    assert not first['escalate']
    result = await _ask(agent_mod, Session, 'уже пробовали')
    assert result['escalate']
    assert result['escalation_reason'] == 'diagnostics_failed'
    assert 'сервер' not in result['answer']
    assert len(captured['prompts']) == 1


@pytest.mark.asyncio
async def test_long_outage_303_is_handed_over_on_the_first_message(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = TEMPLATE
    result = await _ask(agent_mod, Session, 'впн работает только с wi-fi. как только переключаешься на сотовые данные, '
                                            'все сервера недоступны. проблема длится уже около недели.')
    assert result['escalate']
    assert result['escalation_reason'] == 'diagnostics_failed'
    assert not captured['prompts']


@pytest.mark.asyncio
async def test_known_happ_client_still_gets_its_extra_step_once(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = TEMPLATE
    await _ask(agent_mod, Session, 'Happ не подключается')
    recovery = await _ask(agent_mod, Session, 'не помогло')
    assert not recovery['escalate']
    assert 'Сброс пользовательских настроек' in recovery['answer']
    result = await _ask(agent_mod, Session, 'всё равно не работает')
    assert result['escalate']
    assert result['escalation_reason'] == 'diagnostics_failed'


# 3. «Я помогаю только с SligVPN» on an on-topic question.

@pytest.mark.parametrize(('question', 'answer', 'expected'), [
    # #309
    ('Есть возможность добавить Японию? Мне нужна она, а покупать каждый раз другой впн только для неё поднадоело',
     'Здравствуйте. Я помогаю только с SligVPN.\n\nПо подписке у вас уже есть доступ к странам из текущего аккаунта.',
     'Здравствуйте.\n\nПо подписке у вас уже есть доступ к странам из текущего аккаунта.'),
    # #257
    ('Один сервер по обходу блокировок это только один, можете ли добавить сервера',
     'Здравствуйте. Я помогаю только с SligVPN. Если нужен выбор серверов, откройте подписку.',
     'Здравствуйте.\n\nЕсли нужен выбор серверов, откройте подписку.'),
    # #196
    ('Привет, тут есть прокси? Отдельный',
     'Здравствуйте! Я помогаю с SligVPN.\n\nЕсли вы про отдельный прокси, у нас это не отдельная услуга.',
     'Здравствуйте!\n\nЕсли вы про отдельный прокси, у нас это не отдельная услуга.'),
    ('Добавьте Казахстан', 'Я помогаю только с SligVPN. Напишите вопрос по сервису.\nСписок стран виден в подписке.',
     'Список стран виден в подписке.'),
])
def test_scope_disclaimer_is_dropped_from_on_topic_answers(question, answer, expected):
    assert drop_scope_disclaimer(answer, question) == expected


@pytest.mark.parametrize(('question', 'answer'), [
    # #323: off-topic text keeps the refusal
    ('Гондон 1010', 'Здравствуйте!\n\nЯ помогаю только с SligVPN.\nНапишите, пожалуйста, вопрос по сервису.'),
    # nothing else in the answer: keep it
    ('Сколько стоит подписка?', 'Я помогаю только с SligVPN. Задайте вопрос по сервису.'),
    ('Есть Япония?', 'Здравствуйте. Я помогаю только с SligVPN.'),
])
def test_scope_disclaimer_stays_when_it_is_the_whole_answer_or_off_topic(question, answer):
    assert drop_scope_disclaimer(answer, question) == answer


@pytest.mark.asyncio
async def test_japan_question_309_is_answered_without_the_disclaimer(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = ('Здравствуйте. Я помогаю только с SligVPN.\n\n'
                         'Список стран виден в вашей подписке; добавление страны вручную не обещаю.')
    result = await _ask(agent_mod, Session, 'Есть возможность добавить Японию? Мне нужна она, а покупать каждый раз '
                                            'другой впн только для неё поднадоело')
    assert 'Я помогаю' not in result['answer']
    assert 'Список стран виден' in result['answer']


def test_policy_has_new_facts_and_keeps_the_closing_line_last():
    from ai_support_bot.app.services.support_policy import SUPPORT_POLICY

    assert 'Tizen' in SUPPORT_POLICY and 'Android TV' in SUPPORT_POLICY and 'роутер' in SUPPORT_POLICY
    assert '«Россия · Банки»' in SUPPORT_POLICY and '<server_status>' in SUPPORT_POLICY
    assert 'Я помогаю только с SligVPN' in SUPPORT_POLICY
    last = SUPPORT_POLICY.strip().splitlines()[-1]
    assert last.startswith('Отвечай кратко')
    assert last.endswith('Скриншот проси только для обсуждаемого сбоя, если он нужен для диагностики.')


# 4. No grammatical gender in the assistant's own words.

@pytest.mark.parametrize(('text', 'expected'), [
    ('Понял. Тогда проверьте так:', 'Понятно. Тогда проверьте так:'),
    ('Здравствуйте! Поняла, сейчас подскажу.', 'Здравствуйте! Понятно, сейчас подскажу.'),
    ('Поняла вас.', 'Понятно.'),
    ('🎧 Понял, передаю.', '🎧 Понятно, передаю.'),
    ('<b>Понял.</b> Дальше так.', '<b>Понятно.</b> Дальше так.'),
    ('✅ Готово, рада помочь.', '✅ Готово, обращайтесь.'),
    ('Рад помочь!', 'Обращайтесь!'),
    ('Буду рада помочь, если появятся вопросы.', 'Обращайтесь, если появятся вопросы.'),
    ('Буду рада помочь с настройкой.', 'Помогу с настройкой.'),
    ('Готова помочь!', 'Обращайтесь!'),
    ('Если хотите, готов подсказать, как подключить.', 'Если хотите, подскажу, как подключить.'),
    ('Рад, что всё заработало!', 'Хорошо, что всё заработало!'),
    ('- Я проверил доступ: активной оплаченной подписки сейчас не видно.',
     '- Проверка доступа показала: активной оплаченной подписки сейчас не видно.'),
    ('Здравствуйте. Проверил: у вас активная платная подписка.', 'Здравствуйте. Проверка показала: у вас активная платная подписка.'),
    ('Проверила по аккаунту: всё в порядке.', 'Проверка по аккаунту показала: всё в порядке.'),
    ('Я проверила вашу подписку: она активна.', 'Проверка вашей подписки показала: она активна.'),
    ('Да, я проверила: подписка активна.', 'Да, проверка показала: подписка активна.'),
    ('Проверила, всё в порядке.', 'Проверено, всё в порядке.'),
    ('Я передал обращение команде.', 'Обращение передано команде.'),
    ('Передала ваш вопрос команде.', 'Ваш вопрос передан команде.'),
    ('Я передала вашу заявку.', 'Ваша заявка передана.'),
    ('Передала команде, ожидайте.', 'Передано команде, ожидайте.'),
    ('Я рад помочь.', 'Обращайтесь.'),
    ('Если что, я всегда рада помочь!', 'Если что, обращайтесь!'),
    ('Если хотите, я готова подсказать шаги.', 'Если хотите, подскажу шаги.'),
    ('Я не смогла найти платёж.', 'Не удалось найти платёж.'),
    ('Я должна уточнить данные.', 'Нужно уточнить данные.'),
])
def test_gendered_self_reference_is_neutralised(text, expected):
    assert neutral_self_reference(text) == expected


@pytest.mark.parametrize('text', [
    'Вы поняли?',
    'Проверил ли бот оплату?',
    'Понял ли бот вопрос?',
    'Подписка продлена.',
    'Должна быть ссылка в кабинете.',
    'Доступ должен быть уже открыт.',
    'Бот проверил: оплата есть.',
    'Если вы проверили: сервер выбран?',
    'Если хотите, чтобы я проверил дальше, пришлите скриншот.',
    'Ссылка готова.',
    'Уточнила: вы про сайт или бот?',
    'Мы рады помочь.',
])
def test_other_words_are_left_alone(text):
    assert neutral_self_reference(text) == text


@pytest.mark.asyncio
async def test_model_answer_is_neutralised_in_the_flow(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = 'Понял. Ссылка подписки есть в кабинете.'
    result = await _ask(agent_mod, Session, 'Где посмотреть ссылку от моего впн для приложения?')
    assert captured['prompts']
    assert 'Понял' not in result['answer']
    assert 'Понятно. Ссылка подписки есть в кабинете.' in result['answer']


# 5. A «Подключить устройство» answer gets its button.

def test_connect_section_is_recognised_in_the_answer():
    assert mentions_connect('Откройте подписку и нажмите <b>Подключить устройство</b>.')
    assert mentions_connect('Сайт: https://web.slig.app/connection → выберите подписку')
    assert not mentions_connect('Откройте https://web.slig.app/subscriptions')


@pytest.mark.asyncio
async def test_answer_naming_connect_device_gets_the_button(flow):
    captured, agent_mod, Session = flow
    from ai_support_bot.app.services import settings_store
    settings_store._cache['NAVIGATION_ENABLED'] = '0'
    captured['reply'] = 'Откройте свою подписку и нажмите «Подключить устройство», затем импортируйте ссылку в Incy.'
    result = await _ask(agent_mod, Session, 'Приложение INCY скачали, что делать дальше?')
    assert captured['prompts']
    assert _labels(result) == ['Подключить устройство']
    assert result['buttons'][0][0]['url'] == 'https://web.slig.app/connection'


@pytest.mark.asyncio
async def test_connect_button_is_not_added_to_a_handoff(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = 'Передаю обращение, а пока откройте «Подключить устройство».\n' + ESCALATION_MARKER
    result = await _ask(agent_mod, Session, 'Почему сервер Нидерланды пропал из подписки?')
    assert result['escalate']
    assert result['buttons'] == []


# 7. Wheel dates in Moscow time.

def test_wheel_dates_are_shown_in_moscow_time():
    assert moscow_time('2026-11-14 19:17:13.570707+00:00') == '14.11.2026 22:17 МСК'
    assert moscow_time(datetime(2026, 11, 21, 19, 17, 13, tzinfo=UTC)) == '21.11.2026 22:17 МСК'
    assert moscow_time('2026-11-21T19:17:13Z') == '21.11.2026 22:17 МСК'
    assert moscow_time('06.11.2026') == '06.11.2026'
    answer, handoff = wheel_reward_answer({'status': 'ok', 'incomplete_rewards': False, 'rewards': [{
        'code': 'WHEEL914CD238', 'won_at': '2026-11-14 19:17:13.570707+00:00',
        'valid_until': '2026-11-21 19:17:13.568225+00:00', 'usable': True,
    }]})
    assert not handoff
    assert 'выигран 14.11.2026 22:17 МСК; доступен, срок до 21.11.2026 22:17 МСК.' in answer
    assert '+00:00' not in answer and '.570707' not in answer


# 8. One greeting even when the model already greets after the lead emoji.

def test_lead_emoji_before_the_models_greeting_is_not_a_second_greeting():
    answer = decorate('Здравствуйте. Это похоже на спорное списание, такое обращение передаю на проверку.', lead='🎧')
    assert initial_greeting(answer, True) == answer


@pytest.mark.asyncio
async def test_model_greeting_after_handoff_emoji_greets_once(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = 'Здравствуйте! Передаю вопрос про телевизор на проверку.\n' + ESCALATION_MARKER
    result = await _ask(agent_mod, Session, 'VPN не подключается на телевизоре LG')
    assert result['escalate']
    assert 'tg-emoji' in result['answer']
    assert result['answer'].count('Здравствуйте') == 1
    assert not result['answer'].startswith('Здравствуйте!\n\n')


@pytest.mark.parametrize(
    ('evidence', 'claimed'),
    [
        # Т#215: a VPN complaint that mentions the bought subscription is not a payment claim.
        ('Почему в период моей купленной подписки не разу не работали белые списки, при этом пинг показывает , '
         'но при включении ничего не грузит', False),
        ('Оплатил подписку, а VPN не работает', True),
        ('Заказ завершён, а услуга всё ещё не появилась', True),
        ('Купил подписку, где ссылка?', True),
        ('Не работает VPN', False),
    ],
)
def test_payment_tools_need_a_payment_claim(evidence, claimed):
    from ai_support_bot.app.services.agent import _payment_claimed

    assert _payment_claimed(evidence) is claimed


@pytest.mark.parametrize(
    ('evidence', 'claimed'),
    [
        ('нет выбора страны', False),
        ('В приложении нет выбора страны, помогите', False),
        ('Телеграм не работает, потому что нет впн', False),
        ('Айфон, Incy не находится в App Store', True),
        ('Помогаю коллегам поставить, но инси и хапп не доступны в эпстор в рф', True),
        ('не могу скачать приложение на ios', True),
        ('Айфон приложение incy как установить', True),
        ('Как сменить регион Apple ID?', True),
        ('Не ставится приложение на айфон, что делать', True),
        ('Можно оставить заявку?', False),
    ],
)
def test_store_help_needs_a_store_claim(evidence, claimed):
    from ai_support_bot.app.services.agent import _store_claimed

    assert _store_claimed(evidence) is claimed
