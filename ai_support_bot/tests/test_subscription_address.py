"""The subscription address moved on 23.11.2026: connect.sligvpn.ru → sub.slig.my. Phrases are the customers' own."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ai_support_bot.app.core.config import DEFAULT_SYSTEM_PROMPT
from ai_support_bot.app.services import answer_buttons
from ai_support_bot.app.services.agent import convert_markdown_to_html
from ai_support_bot.app.services.service_identity import classify_service_source
from ai_support_bot.app.services.subscription_address import (
    active_subscriptions,
    address_answer,
    address_problem,
    current_link,
    own_codes,
    paid_answer,
    paid_claim,
    recent_subscription_payment,
)
from ai_support_bot.app.services.support_policy import SUPPORT_POLICY
from ai_support_bot.app.services.telegram_html import (
    drop_removed_link_lines,
    is_allowed_link,
    keep_own_subscription_links,
    sanitize_telegram_html,
)
from ai_support_bot.tests.test_live_review_fixes import flow  # noqa: F401  (fixture)

LINK = 'https://sub.slig.my/AbCd1234Efgh'
NOW = datetime.now(UTC)


def _sub(number=1, sub_id=101, until='15.12.2026', link=LINK, status='active', trial='нет', left=' (осталось 24 дн.)'):
    return (f'  • подписка №{number} (id={sub_id}), статус={status}, триал={trial}, до {until}{left}, трафик=безлимит, '
            'устройств=5 (настроенный лимит, не количество подключённых), автоплатеж=выкл'
            + (f', ссылка={link}' if link else '') + '\n    Тариф: Стандарт; включено мест=5')


def _operation(kind='subscription_payment', days_ago=1, status='завершена'):
    moment = (NOW - timedelta(days=days_ago)).strftime('%d.%m.%Y %H:%M UTC')
    return f'  • {moment} {kind} 219.00 ₽ — Продление подписки на 30 дней; статус={status}, id=123, идентификатор=нет'


def _context(*subscriptions, operations=()):
    lines = ['Telegram ID: 4242', 'Баланс: 0.00 ₽', 'Подписки:', *subscriptions]
    if operations:
        lines += ['Последние операции (до 5; отсутствие записи не доказывает отсутствие платежа):', *operations]
    return '\n'.join(lines)


ONE = _context(_sub())
EXPIRED = _context(_sub(status='expired', until='20.11.2026', left=' (истекла 3 дн. назад)'))
PAID_YESTERDAY = _context(_sub(), operations=[_operation()])


# 1. What the customers wrote on 23.11 -----------------------------------------------------------------------------

@pytest.mark.parametrize(('question', 'kind'), [
    ('доброго дня не могу обновить', 'refresh'),
    ('Ошибка обновления подписки. Данные подписки не были получены, превышен таймаут', 'refresh'),
    ('Ошибка: Network error: Read timed out', 'refresh'),
    ('Нет серверов', 'refresh'),
    ('Я не понимаю, как серверы есть если написано на каждый сервер n/a', 'refresh'),
    ('🔄 Обновите подписку — 1 минута 🛑Мы сменили адрес подписки: прежний домен начал блокироваться РКН…', 'news'),
    ('Не там что то прислали, что нужно что то перевыпустить или обновить подписку', 'news'),
    ('Вы поменяли адрес? Где новая ссылка?', 'news'),
    ('Приветствую, как получить ссылку для подключения на продлённую подписку? На ПК не продлился автоматически', 'stale'),
    ('Продлил подписку, а в приложении старая дата', 'stale'),
])
def test_the_moved_address_is_recognised(question, kind):
    assert address_problem(question) == kind


@pytest.mark.parametrize('question', [
    'Не работает впн',
    'Медленно работает VPN на телефоне',
    'пишет не удалось распознать конфигурацию',
    'Почему сервер Нидерланды пропал из подписки?',
    # From the 15.11 replay: N/A on one server among ping jumps is a speed complaint.
    'В официальном приложении я могу сидеть на сервере, где пинг по замерам составляет 80, а потом он резко '
    'становится от 200 до 800 либо вообще пишет N/A',
    'Вчера продлила подписку на 90 дней. В личном кабинете правильно отображается дата, но при этом написано что '
    'подписка истекла.',
    'Не могу обновить приложение',
    'Не обновляется баланс',
    'Положила деньги на счёт, а дни в приложении не увеличились',
    'Оплатил продление, а на сайте висит, что завтра истекает',
    # A real reissue: the customer wants to cut off someone else's devices.
    'Дайте новую ссылку, чужие устройства подключились',
    'Как перевыпустить ссылку? Кто-то пользуется моей подпиской',
    # A timeout while connecting to a server is a server question, not the subscription refresh.
    'Ошибка таймаут при подключении к серверу',
    'connection timed out',
])
def test_other_questions_are_not_the_moved_address(question):
    assert address_problem(question) is None


# 2. Address constants and the customer's data ---------------------------------------------------------------------

def test_old_links_move_to_the_new_host_with_the_same_code():
    assert current_link('https://connect.sligvpn.ru/AbCd1234Efgh') == LINK
    assert current_link('connect.sligvpn.ru/AbCd1234Efgh') == 'sub.slig.my/AbCd1234Efgh'
    assert current_link(LINK) == LINK
    assert current_link('https://web.slig.app/subscriptions') == 'https://web.slig.app/subscriptions'


def test_only_working_subscriptions_with_links_on_the_new_host():
    context = _context(
        _sub(1, 101, link='https://connect.sligvpn.ru/OldHost12345'),
        _sub(2, 102, status='expired', until='01.11.2026', left=' (истекла 22 дн. назад)', link='https://sub.slig.my/Expired1234'),
        _sub(3, 103, trial='да', link=None),
    )
    found = active_subscriptions(context)
    assert [(sub.number, sub.link, sub.trial) for sub in found] == [
        ('1', 'https://sub.slig.my/OldHost12345', False), ('3', None, True)]
    assert own_codes(context) == {'OldHost12345', 'Expired1234'}


def test_completed_subscription_payment_in_the_last_three_days():
    assert recent_subscription_payment(PAID_YESTERDAY)
    assert not recent_subscription_payment(_context(_sub(), operations=[_operation(days_ago=10)]))
    assert not recent_subscription_payment(_context(_sub(), operations=[_operation(kind='deposit')]))
    assert not recent_subscription_payment(_context(_sub(), operations=[_operation(status='не завершена')]))
    assert not recent_subscription_payment(ONE)


def test_links_and_hosts():
    assert is_allowed_link(LINK)
    assert is_allowed_link('https://connect.sligvpn.ru/AbCd1234Efgh')
    assert classify_service_source('', LINK, 'payment_confirmation')['status'] == 'ours'


# 3. Rule «the address moved» ---------------------------------------------------------------------------------------

def test_one_subscription_gets_its_link_in_code_and_no_reissue():
    answer = address_answer(ONE, 'Нет серверов')
    assert answer.text.startswith('🔄 Мы сменили адрес подписки')
    assert f'<code>{LINK}</code>' in answer.text
    assert 'Перевыпускать подписку не нужно.' in answer.text
    assert 'по кнопке ниже' in answer.text
    assert 'срок старый' not in answer.text
    assert answer.links == (LINK,)
    assert answer_buttons.subscription_button(answer.links) == [
        [{'text': 'Моя подписка', 'url': LINK, 'style': 'primary'}]]


def test_several_subscriptions_are_listed_without_a_guessed_button():
    context = _context(_sub(1, 101), _sub(2, 102, link='https://sub.slig.my/XyZ987654321'),
                       _sub(3, 103, link='https://sub.slig.my/Third1234'), _sub(4, 104, link='https://sub.slig.my/Fourth1234'))
    answer = address_answer(context)
    assert f'Подписка №1: <code>{LINK}</code>' in answer.text
    assert 'Подписка №3: <code>https://sub.slig.my/Third1234</code>' in answer.text
    assert 'Fourth1234' not in answer.text  # at most three
    assert 'по кнопке ниже' not in answer.text
    assert answer_buttons.subscription_button(answer.links) == []


def test_a_subscription_without_a_link_is_sent_to_the_bot():
    answer = address_answer(_context(_sub(link=None)))
    assert 'https://t.me/SligVPN_bot' in answer.text and 'Подключить устройство' in answer.text
    assert '<code>' not in answer.text and answer.links == ()


def test_ended_or_missing_subscriptions_keep_their_own_answers():
    assert address_answer(EXPIRED) is None
    assert address_answer('Telegram ID: 4242\nПодписки: отсутствуют') is None
    assert address_answer('') is None


def test_a_renewal_mentioned_asks_to_check_the_bot_too():
    answer = address_answer(ONE, 'Как получить ссылку на продлённую подписку? На ПК не продлился')
    assert answer.text.endswith('Если и в боте или на сайте срок старый — напишите, проверим.')


def test_the_old_host_in_the_data_is_never_shown():
    answer = address_answer(_context(_sub(link='https://connect.sligvpn.ru/AbCd1234Efgh')))
    assert 'connect.sligvpn.ru' not in answer.text and LINK in answer.text


# 4. Rule «paid, the days did not update» when the payment already arrived ----------------------------------------

@pytest.mark.parametrize(('question', 'claim'), [
    ('оплатил, а дни в приложении не обновились', True),
    ('Оплатил продление подписки, а в приложении всё ещё старая дата', True),
    ('Продлил, но подписка в Happ не продлилась', True),
    ('Оплатила в боте, а дни не добавились', True),
    ('Добрый день, купил подписку и почему то не пришла подписка', True),
    ('Оплатила подписку на сайте, на почту письмо не пришло', False),
    ('Вчера продлила подписку. В личном кабинете правильно отображается дата, но написано что подписка истекла', False),
    ('Положила деньги на счёт, а дни не увеличились', False),
    ('Пополнил баланс, а подписка не продлилась', False),
    ('Оплатил продление, а на сайте висит, что завтра истекает', False),
    ('В боте старая дата, хотя я оплатил', False),
    ('Списали дважды, а дни не обновились', False),
    ('Хочу вернуть деньги, оплатил, а дни не добавились', False),
    ('Как добавить устройство?', False),
])
def test_paid_claim(question, claim):
    assert paid_claim(question) is claim


def test_paid_answer_needs_a_recent_completed_payment_and_a_paid_subscription():
    answer = paid_answer(PAID_YESTERDAY)
    assert answer.text.startswith('✅ Оплата дошла: подписка №1 активна до 15.12.2026.')
    assert f'<code>{LINK}</code>' in answer.text
    assert answer.text.endswith('Если и в боте или на сайте срок старый — напишите, проверим.')
    assert paid_answer(ONE) is None
    assert paid_answer(_context(_sub(), operations=[_operation(kind='deposit')])) is None
    assert paid_answer(_context(_sub(), operations=[_operation(days_ago=10)])) is None
    assert paid_answer(_context(_sub(trial='да'), operations=[_operation()])) is None
    assert paid_answer(_context(_sub(status='expired', left=' (истекла 1 дн. назад)'), operations=[_operation()])) is None


def test_several_paid_subscriptions_are_each_named():
    context = _context(_sub(1, 101), _sub(2, 102, until='03.01.2027', link='https://sub.slig.my/XyZ987654321'),
                       operations=[_operation()])
    assert paid_answer(context).text.startswith('✅ Оплата дошла, подписки активны: №1 до 15.12.2026, №2 до 03.01.2027.')


# 5. Only the customer's own subscription link in an answer ---------------------------------------------------------

def test_foreign_subscription_links_from_old_tickets_are_removed():
    codes = own_codes(ONE)
    text = ('Ваша подписка: https://connect.sligvpn.ru/AbCd1234Efgh\n'
            'Вот ссылка: https://connect.sligvpn.ru/SomeoneElse99\n'
            'Новая: <code>https://sub.slig.my/SomeoneElse99</code>\n'
            f'И ещё <a href="https://sub.slig.my/SomeoneElse99">подписка</a>, своя <code>{LINK}</code>.')
    result = drop_removed_link_lines(keep_own_subscription_links(text, codes))
    assert 'SomeoneElse99' not in result
    assert 'connect.sligvpn.ru' not in result
    assert result.splitlines() == [
        f'Ваша подписка: {LINK}', f'И ещё подписка, своя <code>{LINK}</code>.']


def test_own_link_in_an_anchor_moves_to_the_new_host():
    text = '<a href="https://connect.sligvpn.ru/AbCd1234Efgh">Подключить</a> или sub.slig.my/AbCd1234Efgh'
    assert keep_own_subscription_links(text, own_codes(ONE)) == (
        f'<a href="{LINK}">Подключить</a> или sub.slig.my/AbCd1234Efgh')


def test_without_customer_data_every_coded_subscription_link_goes():
    text = 'Ссылка: https://sub.slig.my/AbCd1234Efgh\nСтарый адрес connect.sligvpn.ru больше не открывается.'
    assert drop_removed_link_lines(keep_own_subscription_links(text, set())) == (
        'Старый адрес connect.sligvpn.ru больше не открывается.')


def test_subscription_link_stays_in_code_to_be_copied_with_one_tap():
    assert sanitize_telegram_html(f'<code>{LINK}</code>') == f'<code>{LINK}</code>'
    assert sanitize_telegram_html('<code>https://web.slig.app/balance</code>') == 'https://web.slig.app/balance'
    # «__» inside a code is not Markdown bold.
    assert convert_markdown_to_html('<code>https://sub.slig.my/a__b__c</code> **ок**') == (
        '<code>https://sub.slig.my/a__b__c</code> <b>ок</b>')


def test_my_subscription_button_opens_the_new_host():
    old = '  • подписка №1 (id=57923), статус=active, ссылка=https://connect.sligvpn.ru/AbC123'
    assert answer_buttons.build(['subscription'], old)[0][0]['url'] == 'https://sub.slig.my/AbC123'
    assert answer_buttons.build(['subscription'], ONE)[0][0]['url'] == LINK


# 6. Rules for the model ---------------------------------------------------------------------------------------------

def test_policy_knows_the_new_address_and_keeps_reissue_for_foreign_devices():
    assert 'с 23.11.2026 подписка открывается по адресу sub.slig.my' in SUPPORT_POLICY
    assert 'не советуй перевыпуск: он отключит все устройства' in SUPPORT_POLICY
    assert 'только если клиент сам хочет отключить чужие устройства или сменить ссылку' in SUPPORT_POLICY
    assert 'не перевыпускай ключ без необходимости' not in SUPPORT_POLICY
    assert SUPPORT_POLICY.strip().splitlines()[-1].startswith('Отвечай кратко')
    exported = (Path(__file__).resolve().parents[1] / 'SYSTEM_PROMPT.txt').read_text(encoding='utf-8')
    assert exported.rstrip('\n') == DEFAULT_SYSTEM_PROMPT + '\n\n' + SUPPORT_POLICY


# 7. The whole agent -------------------------------------------------------------------------------------------------

async def _send(agent_mod, Session, question, **kwargs):
    async with Session() as db:
        return await agent_mod.support_agent.generate_answer(db, 4242, question, **kwargs)


async def _state(agent_mod, Session):
    async with Session() as db:
        return dict((await agent_mod.crud.get_or_create_conversation(db, 4242)).support_state or {})


def _use(monkeypatch, agent_mod, context):
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=context))


def _is_address_rule(result):
    return 'Мы сменили адрес подписки' in result['answer'] and result['model'] == 'rules'


@pytest.mark.asyncio
async def test_cannot_update_after_a_screenshot_gets_the_new_link(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    captured['reply'] = 'Похоже, это Happ. Обновите подписку в приложении и перезапустите его.'
    await _send(agent_mod, Session, '', image_url='https://example.invalid/happ-error')
    prompts = len(captured['prompts'])
    result = await _send(agent_mod, Session, 'доброго дня не могу обновить')
    assert len(captured['prompts']) == prompts  # no model call
    assert _is_address_rule(result) and not result['escalate']
    assert f'<code>{LINK}</code>' in result['answer']
    assert result['buttons'] == [[{'text': 'Моя подписка', 'url': LINK, 'style': 'primary'}]]
    assert not result['answer'].startswith('Здравствуйте')


@pytest.mark.asyncio
@pytest.mark.parametrize('question', [
    'Ошибка: Network error: Read timed out',
    'Нет серверов',
    'Я не понимаю, как серверы есть если написано на каждый сервер n/a',
    '🔄 Обновите подписку — 1 минута 🛑Мы сменили адрес подписки: прежний домен начал блокироваться РКН…',
])
async def test_refresh_errors_and_the_mailing_get_the_rule(flow, monkeypatch, question):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    result = await _send(agent_mod, Session, question)
    assert not captured['prompts']
    assert _is_address_rule(result) and not result['escalate']
    assert result['answer'].startswith('Здравствуйте!\n\n🔄 Мы сменили адрес подписки')
    assert LINK in result['answer']


@pytest.mark.asyncio
async def test_reissue_or_refresh_question_is_not_told_to_reissue(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    result = await _send(agent_mod, Session, 'Не там что то прислали, что нужно что то перевыпустить или обновить подписку')
    assert _is_address_rule(result)
    assert 'Перевыпускать подписку не нужно.' in result['answer']
    assert 'Перевыпустить' not in result['answer'] and 'перевыпустите' not in result['answer'].casefold()


@pytest.mark.asyncio
async def test_renewed_subscription_link_for_the_pc(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    result = await _send(agent_mod, Session, 'Приветствую, как получить ссылку для подключения на продлённую подписку? '
                                             'На ПК не продлился автоматически')
    assert _is_address_rule(result) and not result['escalate']
    assert f'<code>{LINK}</code>' in result['answer']
    assert 'Если и в боте или на сайте срок старый' in result['answer']


@pytest.mark.asyncio
@pytest.mark.parametrize('question', ['Не работает впн', 'пишет не удалось распознать конфигурацию'])
async def test_ordinary_failures_go_to_the_model(flow, monkeypatch, question):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    captured['reply'] = 'Перезапустите приложение и выберите другой сервер.'
    result = await _send(agent_mod, Session, question)
    assert captured['prompts']
    assert 'сменили адрес' not in result['answer']


@pytest.mark.asyncio
async def test_ended_subscription_keeps_its_answer(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, EXPIRED)
    result = await _send(agent_mod, Session, 'Ошибка обновления подписки, VPN не работает')
    assert 'сменили адрес' not in result['answer']
    assert 'Подписка закончилась 20.11.2026' in result['answer']


@pytest.mark.asyncio
async def test_no_subscription_no_rule(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, 'Telegram ID: 4242\nБаланс: 0.00 ₽\nПодписки: отсутствуют')
    result = await _send(agent_mod, Session, 'Нет серверов')
    assert captured['prompts']
    assert 'сменили адрес' not in result['answer']


@pytest.mark.asyncio
async def test_rule_answers_once_per_dialogue(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    assert _is_address_rule(await _send(agent_mod, Session, 'Нет серверов'))
    second = await _send(agent_mod, Session, 'Добавил заново, всё равно нет серверов')
    # The usual diagnostics go on (here the Happ recovery step asks for the app), not the same steps again.
    assert 'сменили адрес' not in second['answer']
    assert 'Какое VPN-приложение вы используете?' in second['answer']
    captured['reply'] = 'Пришлите, пожалуйста, скриншот ошибки.'
    third = await _send(agent_mod, Session, 'Ошибка: Network error: Read timed out')
    assert captured['prompts'] and 'сменили адрес' not in third['answer']


@pytest.mark.asyncio
async def test_money_dispute_keeps_the_handoff(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, PAID_YESTERDAY)
    result = await _send(agent_mod, Session, 'Ошибка обновления подписки, верните деньги')
    assert result['escalate'] and result['escalation_reason'] == 'refund'
    assert 'сменили адрес' not in result['answer'] and 'Оплата дошла' not in result['answer']


@pytest.mark.asyncio
async def test_failed_diagnostics_round_gives_way_to_the_address(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    result = await _send(agent_mod, Session, 'Уже неделю нет серверов, всё пробовал')
    assert _is_address_rule(result) and not result['escalate']


@pytest.mark.asyncio
async def test_paid_yesterday_and_the_app_shows_old_days(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, PAID_YESTERDAY)
    result = await _send(agent_mod, Session, 'оплатил, а дни в приложении не обновились')
    assert not captured['prompts']
    assert result['model'] == 'rules' and not result['escalate']
    assert 'Оплата дошла' in result['answer'] and 'активна до 15.12.2026' in result['answer']
    assert f'<code>{LINK}</code>' in result['answer']
    assert result['buttons'] == [[{'text': 'Моя подписка', 'url': LINK, 'style': 'primary'}]]
    state = await _state(agent_mod, Session)
    assert not state.get('renewal_verification') and state.get('issue') not in {'payment_activation', 'payment_missing'}


@pytest.mark.asyncio
async def test_paid_renewal_claim_drops_the_sticky_payment_flags(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, PAID_YESTERDAY)
    result = await _send(agent_mod, Session, 'Оплатил продление подписки, а в приложении всё ещё старая дата')
    assert 'Оплата дошла' in result['answer'] and 'чек' not in result['answer']
    state = await _state(agent_mod, Session)
    assert not state.get('renewal_verification') and state.get('issue') not in {'payment_activation', 'payment_missing'}


@pytest.mark.asyncio
@pytest.mark.parametrize(('question', 'context'), [
    ('оплатил, а дни в приложении не обновились', ONE),
    ('оплатил, а дни в приложении не обновились', _context(_sub(), operations=[_operation(kind='deposit')])),
    ('оплатил, а дни в приложении не обновились', _context(_sub(), operations=[_operation(days_ago=10)])),
    ('Положила деньги на счёт, а дни не увеличились', PAID_YESTERDAY),
    ('Оплатил продление, а на сайте висит, что завтра истекает', PAID_YESTERDAY),
])
async def test_payment_rule_does_not_fire_without_a_fresh_renewal(flow, monkeypatch, question, context):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, context)
    captured['reply'] = 'Проверим оплату.'
    result = await _send(agent_mod, Session, question)
    assert 'Оплата дошла' not in result['answer']


@pytest.mark.asyncio
async def test_unconfirmed_payment_with_an_old_date_in_the_app_still_gets_the_steps(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, ONE)
    result = await _send(agent_mod, Session, 'оплатил, а дни в приложении не обновились')
    assert _is_address_rule(result)
    assert 'Если и в боте или на сайте срок старый — напишите, проверим.' in result['answer']


def _service_check(seller, url, location):
    arguments = json.dumps({'seller_name': seller, 'seller_url': url, 'location': location})
    return {'content': None, 'tool_calls': [{'id': 'call-source', 'type': 'function',
                                             'function': {'name': 'verify_service_source', 'arguments': arguments}}]}


@pytest.mark.asyncio
async def test_our_receipt_photo_gets_the_payment_answer_after_the_check(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, PAID_YESTERDAY)
    captured['responses'] = [_service_check('SligVPN', 'https://t.me/SligVPN_bot', 'payment_confirmation'),
                             {'content': 'Пришлите чек этой операции.'}]
    result = await _send(agent_mod, Session, 'Оплатил, а дни в приложении не обновились', image_url='https://example.invalid/receipt')
    assert captured['prompts']
    assert result['answer'].startswith('Здравствуйте!') and 'Оплата дошла' in result['answer']
    assert not result['escalate'] and 'чек' not in result['answer']
    assert result['buttons'] == [[{'text': 'Моя подписка', 'url': LINK, 'style': 'primary'}]]


@pytest.mark.asyncio
async def test_another_services_receipt_photo_is_not_told_the_payment_arrived(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, PAID_YESTERDAY)
    captured['responses'] = [_service_check('Other VPN', 'https://t.me/othervpn_bot', 'seller_chat'),
                             {'content': 'Это оплата другого сервиса.'}]
    result = await _send(agent_mod, Session, 'Оплатил, а дни в приложении не обновились', image_url='https://example.invalid/receipt')
    assert 'Оплата дошла' not in result['answer']


@pytest.mark.asyncio
async def test_model_answer_keeps_only_the_customers_own_link(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    _use(monkeypatch, agent_mod, _context(_sub(link='https://connect.sligvpn.ru/AbCd1234Efgh')))
    captured['reply'] = ('Ваша ссылка: https://connect.sligvpn.ru/AbCd1234Efgh\n'
                         'Пример из прошлого обращения: https://connect.sligvpn.ru/SomeoneElse99\n'
                         'Добавьте её в приложение.')
    result = await _send(agent_mod, Session, 'Как добавить подписку в приложение?')
    assert 'SomeoneElse99' not in result['answer'] and '[ссылка удалена]' not in result['answer']
    assert f'Ваша ссылка: {LINK}' in result['answer']
    assert 'connect.sligvpn.ru' not in result['answer']
