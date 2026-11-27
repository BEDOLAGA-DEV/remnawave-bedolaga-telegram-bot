"""«Авто выбор» first, only Happ and Incy, the moved address, payment templates and «всё равно не работает» (25–27.11.2026).

The phrases are the customers' own, with personal details removed.
"""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_support_bot.app.core.config import DEFAULT_SYSTEM_PROMPT
from ai_support_bot.app.services import dialogue_tools
from ai_support_bot.app.services import server_status as ss
from ai_support_bot.app.services.response_rules import (
    APPLE_ID_ALTERNATIVE,
    AUTO_SELECT_ANSWER,
    CLIENTS,
    HAPP_RECOVERY,
    NOTICE,
    OUTAGE_STEP,
    PAYMENT_TROUBLE_ANSWER,
    SHADOWROCKET_ANSWER,
    apps_answer,
    auto_select_only,
    basic_steps_given,
    conversation_topic,
    handoff_answer,
    mandatory_handoff,
    paid_access_request,
    paid_period_complaint,
    payment_trouble,
    starts_new_topic,
    store_alternatives_answer,
)
from ai_support_bot.app.services.subscription_address import address_problem
from ai_support_bot.app.services.support_policy import SUPPORT_POLICY
from ai_support_bot.tests.test_live_review_fixes import _ask, flow  # noqa: F401  (fixture)

BANNED_CLIENTS = ('Streisand', 'V2RayNG', 'v2rayN', 'V2Box')
ACTIVE = ('Telegram ID: 4242\nБаланс: 0.00 ₽\nПодписки:\n'
          '  • подписка №1 (id=11), статус=active, триал=нет, до 15.12.2026, устройств=5, '
          'ссылка=https://sub.slig.my/AbCd1234Efgh')


def _history(*pairs):
    return [SimpleNamespace(role=role, content=content) for role, content in pairs]


def _rule(title):
    return next(line for line in SUPPORT_POLICY.splitlines() if line.startswith(title))


async def _send(agent_mod, Session, question, **kwargs):
    async with Session() as db:
        return await agent_mod.support_agent.generate_answer(db, 4242, question, **kwargs)


async def _state(agent_mod, Session):
    async with Session() as db:
        return dict((await agent_mod.crud.get_or_create_conversation(db, 4242)).support_state or {})


def _tool_reply(name):
    return {'content': None, 'tool_calls': [{'id': f'call-{name}', 'type': 'function',
                                            'function': {'name': name, 'arguments': '{}'}}]}


# 1. «Авто выбор» is the first step ----------------------------------------------------------------------------------

def test_policy_makes_auto_select_the_first_step_of_a_failure():
    rule = _rule('Сбой VPN')
    assert 'Первым шагом' in rule and 'локацию «Авто выбор»' in rule
    assert rule.index('«Авто выбор»') < rule.index('перезапуск')
    assert 'смену сервера из его подписки' not in rule
    assert 'Конкретные страны при сбое не перечисляй' in rule
    assert '«Россия · Банки» и «Россия · YouTube» при обычных сбоях не советуй' in rule
    assert 'работает только «Авто выбор», пусть им и пользуется' in rule
    assert 'выбор локации «Авто выбор»' in _rule('Дополнительная диагностика Happ')
    assert 'смена сервера' not in _rule('Дополнительная диагностика Happ')
    assert 'предложи «Авто выбор»' in _rule('Страны и серверы называй')


def test_policy_and_exported_prompt_stay_in_sync():
    assert SUPPORT_POLICY.strip().splitlines()[-1].startswith('Отвечай кратко')
    exported = (Path(__file__).resolve().parents[1] / 'SYSTEM_PROMPT.txt').read_text(encoding='utf-8')
    assert exported.rstrip('\n') == DEFAULT_SYSTEM_PROMPT + '\n\n' + SUPPORT_POLICY


def test_quick_step_of_a_long_outage_is_auto_select():
    assert OUTAGE_STEP.startswith('Пока ждёте: выберите в приложении локацию «Авто выбор»')
    assert 'сервер «Белые списки»' not in OUTAGE_STEP
    assert handoff_answer('diagnostics_failed', 'Уже несколько дней не работает VPN') == NOTICE + '\n\n' + OUTAGE_STEP


@pytest.mark.parametrize('answer', [
    'Выберите в приложении локацию «Авто выбор», перезапустите приложение и проверьте Wi-Fi.',
    'Переключитесь на Авто выбор и обновите подписку в приложении.',
])
def test_auto_select_steps_count_as_the_basic_steps(answer):
    # Otherwise «не помогло» after them would get the same steps again instead of the next one.
    assert basic_steps_given(answer)


SAMPLE = (Path(__file__).resolve().parent / 'fixtures' / 'xray_metrics_sample.txt').read_text(encoding='utf-8')


def _block(question=''):
    records = ss.parse_xray_metrics(SAMPLE)
    status = ss.ServerStatus(checked_at=0, proxies=ss.SourceSnapshot(items=tuple(records), fetched_at=1_763_204_700), nodes=None)
    return ss.format_status_block(status, now=1_763_204_700, question=question)


def test_status_block_tells_the_model_about_the_balancer():
    block = _block('Не работает VPN')
    hint = next(line for line in block.splitlines() if line.startswith('«Авто выбор»'))
    assert 'балансировщик, мониторинг его не показывает' in hint
    assert 'первым советуй его, а не конкретную страну' in hint
    assert '«Россия · Банки» и «Россия · YouTube» — только для российских банков' in hint
    # The balancers themselves are still not listed as servers.
    assert not [line for line in block.splitlines() if line.startswith('•') and 'Авто' in line]


def test_nodes_only_block_carries_the_hint_too():
    nodes = ss.SourceSnapshot(items=(ss.NodeRecord('DE', True),), fetched_at=1_763_204_700)
    block = ss.format_status_block(ss.ServerStatus(checked_at=0, nodes=nodes), now=1_763_204_700)
    assert '«Авто выбор» — балансировщик' in block
    assert 'Россия · Банки' not in block  # not invented without the list


# 1b. «Работает только на автовыборе» ---------------------------------------------------------------------------------

@pytest.mark.parametrize('question', [
    'Впн работает только на автовыборе',
    'только авто выбор работает',
    'работает лишь автовыбор',
    'Работает только «Авто выбор»',
    'Все сервера кроме авто выбора не работают',
    'на автовыборе работает, а на других нет',
])
def test_only_auto_select_works_is_recognised(question):
    assert auto_select_only(question)


@pytest.mark.parametrize('question', [
    'автовыбор не работает',
    'даже авто выбор не работает',
    'Работал только автовыбор, теперь и он не работает',
    'не работает только автовыбор',
    'только автовыбор не работает',
    'Автовыбор тоже не подключается',
])
def test_auto_select_failing_is_not_that_case(question):
    assert not auto_select_only(question)


@pytest.mark.asyncio
async def test_only_auto_select_gets_keep_using_it(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    captured['reply'] = 'Выберите не Автовыбор, а любой другой сервер.'
    result = await _ask(agent_mod, Session, 'Впн работает только на автовыборе')
    assert not captured['prompts']
    assert result['model'] == 'rules' and not result['escalate']
    assert AUTO_SELECT_ANSWER in result['answer']
    assert 'не Автовыбор' not in result['answer'] and 'другой сервер' not in result['answer']


@pytest.mark.asyncio
async def test_only_auto_select_after_the_steps_is_not_a_failed_round(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    captured['reply'] = 'Выберите в приложении локацию «Авто выбор», перезапустите приложение и проверьте Wi-Fi.'
    await _ask(agent_mod, Session, 'Не работает VPN')
    result = await _ask(agent_mod, Session, 'Все сервера кроме авто выбора не работают')
    assert not result['escalate'] and AUTO_SELECT_ANSWER in result['answer']
    # Once per dialogue: a second message about it goes to the model.
    prompts = len(captured['prompts'])
    captured['reply'] = 'Напишите, какая страна нужна, проверю.'
    again = await _ask(agent_mod, Session, 'Но работает только автовыбор, а мне нужна Германия')
    assert len(captured['prompts']) == prompts + 1 and AUTO_SELECT_ANSWER not in again['answer']


# 1c. Mobile internet: «Авто выбор Белые списки» first -----------------------------------------------------------------

def test_mobile_internet_gets_the_whitelist_balancer_first():
    rule = _rule('Обход белых списков')
    assert 'предложи выбрать «Авто выбор Белые списки»' in rule
    assert rule.index('«Авто выбор Белые списки» и обновить') < rule.index('«Белые списки» и «Белые списки #2» называй')
    assert 'там первым шагом «Авто выбор»' in rule
    note = next(line for line in _block('На мобильном интернете не работает, на Wi-Fi работает').splitlines()
                if line.startswith('«Белые списки»'))
    assert 'предложи «Авто выбор Белые списки»' in note
    assert '«Белые списки» и «Белые списки #2» — только если клиент спросил о них' in note
    assert 'Если VPN не работает только на мобильном интернете, выберите «Авто выбор Белые списки»' in OUTAGE_STEP


# 2. Only Happ and Incy ----------------------------------------------------------------------------------------------

def test_client_lists_have_only_happ_and_incy():
    assert set(CLIENTS) == {'Android', 'iPhone/iPad', 'Windows', 'macOS', 'Android TV'}
    assert all(set(apps) == {'Incy', 'Happ'} for apps in CLIENTS.values())


@pytest.mark.parametrize('question', [
    'Какие приложения скачать?', 'Какие приложения для Android?', 'Какие приложения для iPhone?',
    'Какое приложение для Windows?', 'Какие приложения для macOS?', 'Какие приложения для Android TV?',
])
def test_apps_answers_name_no_unsupported_client(question):
    answer = apps_answer(question, [])
    assert 'Happ' in answer and 'Incy' in answer
    assert not any(name.casefold() in answer.casefold() for name in BANNED_CLIENTS + ('Shadowrocket',))


def test_android_tv_is_its_own_platform():
    assert apps_answer('Какие приложения для Android TV?', []) == 'Android TV: Incy, Happ.'
    assert apps_answer('Какие приложения для Android?', []) == 'Android: Incy, Happ.'


def test_policy_names_other_clients_only_as_not_recommended():
    for line in SUPPORT_POLICY.splitlines():
        for sentence in re.split(r'(?<=[.!?])\s+', line):
            if any(name in sentence for name in BANNED_CLIENTS):
                assert 'не рекомендуй' in sentence, sentence
    assert 'в Incy и Streisand' not in SUPPORT_POLICY and 'на iPhone — Streisand' not in SUPPORT_POLICY
    assert 'Android TV и Google TV подключаются через Happ или Incy' in SUPPORT_POLICY


def test_shadowrocket_answer_offers_happ_or_incy_only():
    assert 'Happ' in SHADOWROCKET_ANSWER and 'Incy' in SHADOWROCKET_ANSWER
    assert not any(name in SHADOWROCKET_ANSWER for name in BANNED_CLIENTS)


def test_streisand_stub_is_answered_like_shadowrocket():
    # The live case: the client installed Streisand on our old advice and got «Приложение не поддерживается».
    answer = apps_answer('Поставил Streisand, пишет «Приложение не поддерживается»', [])
    assert answer.startswith('Streisand с нашей подпиской не работает. Для подключения используйте Happ или Incy')
    told = _history(('assistant', answer))
    assert apps_answer('Streisand всё равно не работает', told) is None  # once per dialogue


@pytest.mark.parametrize('question', ['Не хочу менять регион', 'На балансе Apple ID остались деньги, регион не меняется'])
def test_region_refusal_gets_a_separate_apple_id(question):
    answer = apps_answer(question, [])
    assert answer == APPLE_ID_ALTERNATIVE
    assert 'Медиаматериалы и покупки → Выйти' in answer and 'Казахстана' in answer
    assert 'iCloud, фото и контакты останутся' in answer
    assert 'Страна/регион' not in answer  # no second copy of the region steps
    assert not any(name in answer for name in BANNED_CLIENTS)


def test_store_alternatives_have_no_unsupported_client_on_any_platform():
    for question in ('', 'на айфоне', 'на android', 'на Android TV', 'на windows', 'на macos'):
        answer = store_alternatives_answer(question)
        assert not any(name in answer for name in BANNED_CLIENTS + ('Shadowrocket',)), question
    assert 'Happ.apk' in store_alternatives_answer('на android')


@pytest.mark.asyncio
async def test_second_store_complaint_gets_the_apple_id_way(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    await _ask(agent_mod, Session, 'В App Store нет Happ, как установить?')
    second = await _ask(agent_mod, Session, 'Не могу сменить регион, у меня семейный доступ')
    assert APPLE_ID_ALTERNATIVE in second['answer'] and 'Streisand' not in second['answer']
    assert not captured['prompts']


# 3. The moved address in other words (25–27.11) ---------------------------------------------------------------------

@pytest.mark.parametrize(('question', 'kind'), [
    ('Пришла инструкция по обновлению Happ', 'news'),
    ('И то что по последним смс я не могу сделать', 'news'),
    ('вчера пришло уведомление из бота, что из-за атак нужно поменять адрес, для этого надо что-то подключить… '
     'можете продублировать', 'news'),
    ('в приложении пропали страны для выбора', 'refresh'),
    ('нет стран', 'refresh'),
    ('страны исчезли', 'refresh'),
])
def test_new_address_phrases_are_recognised(question, kind):
    assert address_problem(question) == kind


def test_did_everything_by_it_after_the_mailing_is_the_address():
    question = 'Я сделал все по ней, но не получается'
    assert address_problem(question) is None
    assert address_problem(question, 'Пришла инструкция по обновлению Happ') == 'news'
    assert address_problem(question, 'Не работает VPN') is None


@pytest.mark.parametrize('question', [
    'Каких стран нет?',
    'Пропала локация Германия',
    'Нет выбора страны',
    'Как поменять адрес электронной почты?',
    'Как сменить почтовый адрес?',
    'по смс не приходит код',
    'Не пришло смс с кодом',
    'Почему сервер Нидерланды пропал из подписки?',
])
def test_similar_phrases_are_not_the_address(question):
    assert address_problem(question) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('question', ['в приложении пропали страны для выбора', 'Пришла инструкция по обновлению Happ'])
async def test_new_phrases_get_the_address_rule(flow, monkeypatch, question):  # noqa: F811
    captured, agent_mod, Session = flow
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=ACTIVE))
    result = await _ask(agent_mod, Session, question)
    assert not captured['prompts']
    assert result['model'] == 'rules' and 'Мы сменили адрес подписки' in result['answer']
    assert '<code>https://sub.slig.my/AbCd1234Efgh</code>' in result['answer']


@pytest.mark.asyncio
async def test_mailing_steps_failed_with_a_photo_get_the_address_rule(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=ACTIVE))
    result = await _send(agent_mod, Session, 'И то что по последним смс я не могу сделать',
                         image_url='https://example.invalid/mailing')
    assert result['model'] == 'rules' and 'Мы сменили адрес подписки' in result['answer']


# 4. Payment templates out of place ----------------------------------------------------------------------------------

PAID_PERIOD = 'Не работает ваш, не хочу пользоваться другим, ваш ещё месяц проплачен'


@pytest.mark.parametrize(('question', 'technical'), [
    (PAID_PERIOD, True),
    ('Подписка оплачена, а VPN не работает', True),
    ('оплачено до декабря, впн не подключается', True),
    ('Оплатил, а VPN не работает', False),
    ('оплачена, но не продлилась и не работает', False),
    ('Оплатила, деньги списались, подписка не продлилась', False),
])
def test_paid_period_with_a_vpn_failure_is_technical(question, technical):
    assert paid_period_complaint(question) is technical
    if technical:
        assert not paid_access_request(question)


@pytest.mark.parametrize(('question', 'trouble'), [
    ('Почему не могу оплатить?', True),
    ('Не получается оплатить подписку', True),
    ('Не проходит оплата', True),
    ('Ошибка при оплате', True),
    ('не открывается страница оплаты', True),
    ('Оплатила, деньги списались, подписка не продлилась', False),
    ('не могу оплатить, деньги списали', False),
    ('не удаётся оплатить с промокодом', False),
    ('Как оплатить?', False),
])
def test_payment_trouble_is_recognised(question, trouble):
    assert payment_trouble(question) is trouble


def _access(monkeypatch, active_paid=False):
    """The live account read: the access check found no active paid subscription, the receipt template followed."""
    monkeypatch.setattr(dialogue_tools, 'read_account_support',
                        AsyncMock(return_value={'status': 'ok', 'active_paid_subscription': active_paid}))


@pytest.mark.asyncio
async def test_paid_period_complaint_gets_diagnostics_not_a_receipt(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _access(monkeypatch)
    # The model tries the payment check anyway: it is refused for this message.
    captured['responses'] = [_tool_reply('check_paid_access'),
                             {'content': 'Выберите в приложении локацию «Авто выбор» и перезапустите приложение.'}]
    result = await _ask(agent_mod, Session, PAID_PERIOD)
    assert captured['prompts']
    assert '«Авто выбор»' in result['answer']
    assert 'чек' not in result['answer'] and 'дошла ли оплата' not in result['answer']
    assert not result['escalate']
    state = await _state(agent_mod, Session)
    assert state.get('issue') != 'payment_activation' and '_paid_period_complaint' not in state


@pytest.mark.asyncio
async def test_cannot_pay_gets_the_browser_and_other_methods(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _access(monkeypatch)
    captured['responses'] = [_tool_reply('check_paid_access'), {'content': 'Пришлите чек.'}]
    result = await _ask(agent_mod, Session, 'Почему не могу оплатить?')
    assert not captured['prompts']
    assert result['model'] == 'rules' and not result['escalate']
    assert PAYMENT_TROUBLE_ANSWER.splitlines()[0] in result['answer']  # the bot link becomes an anchor later
    assert 'обычном браузере' in result['answer'] and 'СБП' in result['answer'] and 'Telegram Stars' in result['answer']
    assert 'чек' not in result['answer']


@pytest.mark.asyncio
async def test_cannot_pay_with_a_photo_does_not_open_the_payment_check(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _access(monkeypatch)
    captured['responses'] = [_tool_reply('check_paid_access'), {'content': 'На скриншоте ошибка банка: попробуйте СБП.'}]
    result = await _send(agent_mod, Session, 'Не проходит оплата', image_url='https://example.invalid/error')
    assert 'СБП' in result['answer'] and 'дошла ли оплата' not in result['answer']
    assert (await _state(agent_mod, Session)).get('issue') != 'payment_activation'


@pytest.mark.asyncio
async def test_charged_without_renewal_stays_a_payment_check(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _access(monkeypatch)
    result = await _ask(agent_mod, Session, 'Оплатила, деньги списались, подписка не продлилась')
    assert PAYMENT_TROUBLE_ANSWER not in result['answer']
    assert re.search(r'оплачивали|чек', result['answer'])
    state = await _state(agent_mod, Session)
    assert state.get('renewal_verification') or state.get('issue') == 'payment_activation'


# 5. «Всё равно не работает» continues the dialogue -------------------------------------------------------------------

AFTER_RESET = _history(
    ('user', 'Впн не работает'),
    ('assistant', 'Выберите в приложении локацию «Авто выбор», перезапустите приложение и проверьте Wi-Fi и мобильную сеть.'),
    ('user', 'Не помогло, в приложении Happ'),
    ('assistant', HAPP_RECOVERY),
)


@pytest.mark.parametrize('question', [
    'всеравно не работает почему-то', 'всё равно не работает', 'все равно не работает', 'всё ещё не работает',
    'до сих пор не работает', 'Почему не работает?',
])
def test_still_failing_continues_the_dialogue(question):
    assert not starts_new_topic(question, AFTER_RESET, {'issue': 'other'})
    assert mandatory_handoff(question, AFTER_RESET) == 'diagnostics_failed'


def test_why_is_not_the_price_of_something():
    # «Почему-то» once matched «почём» and made a price question out of a VPN failure.
    assert conversation_topic('всеравно не работает почему-то') is None
    assert conversation_topic('Почём годовая подписка?') == 'subs_buy_subscription'


@pytest.mark.asyncio
async def test_after_the_happ_reset_a_still_failing_vpn_goes_to_the_team(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    captured['reply'] = 'Выберите в приложении локацию «Авто выбор», перезапустите приложение и проверьте Wi-Fi и мобильную сеть.'
    first = await _ask(agent_mod, Session, 'Впн не работает')
    reset = await _ask(agent_mod, Session, 'Не помогло, в приложении Happ')
    assert 'Сброс пользовательских настроек' in reset['answer']
    prompts = len(captured['prompts'])
    result = await _ask(agent_mod, Session, 'всеравно не работает почему-то')
    assert result['conversation_id'] == first['conversation_id']
    assert len(captured['prompts']) == prompts  # no new round of the model's steps
    assert result['escalate'] and result['escalation_reason'] == 'diagnostics_failed'
    assert NOTICE in result['answer']
    assert 'Сброс' not in result['answer'] and 'Авто выбор' not in result['answer']
