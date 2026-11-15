from unittest.mock import AsyncMock

import pytest

from ai_support_bot.app.services import answer_buttons
from ai_support_bot.app.services.answer_buttons import SITE, build, extract, page_actions

ONE_SUBSCRIPTION = '  • подписка №1 (id=57923), статус=active, ссылка=https://connect.sligvpn.ru/AbC123'
TWO_SUBSCRIPTIONS = ONE_SUBSCRIPTION + '\n  • подписка №2 (id=60001), статус=active, ссылка=https://connect.sligvpn.ru/XyZ789'


def test_marker_is_removed_and_known_actions_kept_in_order():
    text, actions = extract('Продлить можно в кабинете.\n[[BUTTONS: renew, bot, renew, hack]]')

    assert text == 'Продлить можно в кабинете.'
    assert actions == ['renew', 'bot']


def test_marker_before_escalation_marker_keeps_it_last():
    text, actions = extract('Передаю команде.\n[[BUTTONS: bot]]\n[[ESCALATE]]')

    assert text.endswith('[[ESCALATE]]')
    assert actions == ['bot']


def test_personal_link_and_subscription_pages_come_from_customer_data():
    rows = build(['subscription', 'renew'], ONE_SUBSCRIPTION)

    assert rows == [
        [{'text': 'Моя подписка', 'url': 'https://connect.sligvpn.ru/AbC123', 'style': 'primary'}],
        [{'text': 'Продлить подписку', 'url': f'{SITE}/subscriptions/57923/renew', 'style': 'success'}],
    ]


def test_several_subscriptions_lead_to_the_list_not_to_a_guess():
    rows = build(['subscription', 'renew'], TWO_SUBSCRIPTIONS)

    assert [row[0]['url'] for row in rows] == [f'{SITE}/subscriptions', f'{SITE}/subscriptions']


def test_at_most_two_buttons_and_bot_button_has_no_colour():
    rows = build(['topup', 'buy', 'bot'])

    assert len(rows) == 2
    assert build(['bot']) == [[{'text': 'Открыть бота', 'url': 'https://t.me/SligVPN_bot?start=ai_support'}]]


def test_foreign_subscription_host_is_never_shown(monkeypatch):
    monkeypatch.setattr(answer_buttons, 'is_allowed_link', lambda url: 'evil' not in url)

    assert build(['subscription'], 'подписка №1 (id=1), ссылка=https://evil.example/x') == []


def test_rule_answers_offer_the_page_they_name():
    assert page_actions('На сайте: https://web.slig.app/balance/top-up\nПуть: Баланс') == ['topup']
    assert page_actions('Откройте https://web.slig.app/subscription/purchase') == ['buy']
    assert page_actions('Откройте https://web.slig.app/subscriptions') == []


@pytest.mark.asyncio
async def test_buttons_are_sent_with_colour_and_dropped_on_refusal(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    from ai_support_bot.app.bot import bot as bot_mod

    monkeypatch.setattr(bot_mod, '_turn_active', AsyncMock(return_value=True))
    message = AsyncMock()
    assert await bot_mod._answer_safely(message, 'Ответ', build(['renew'], ONE_SUBSCRIPTION))
    markup = message.answer.await_args.kwargs['reply_markup']
    assert markup.model_dump(exclude_none=True)['inline_keyboard'][0][0]['style'] == 'success'

    message = AsyncMock()
    message.answer.side_effect = [TelegramBadRequest(method=None, message='Bad Request: BUTTON_STYLE_INVALID'), None]
    assert await bot_mod._answer_safely(message, 'Ответ', build(['renew']))
    assert 'reply_markup' not in message.answer.await_args_list[1].kwargs


REAL_CONTEXT = (
    '  • подписка №1 (id=57923), статус=active, триал=нет, до 14.12.2026 (осталось 29 дн.), трафик=∞\n'
    '  • подписка №2 (id=60001), статус=expired, триал=нет, до 07.11.2026 (истекла 8 дн. назад), трафик=∞\n'
    '  • подписка №3 (id=60002), статус=active, триал=да, до 16.11.2026, трафик=∞'
)


def test_renew_buttons_lead_straight_to_each_paid_subscription():
    rows = answer_buttons.renew_buttons(REAL_CONTEXT)

    assert rows == [
        [{'text': 'Продлить №1 · до 14.12.2026', 'url': f'{SITE}/subscriptions/57923/renew', 'style': 'success'}],
        [{'text': 'Продлить №2 · до 07.11.2026', 'url': f'{SITE}/subscriptions/60001/renew', 'style': 'success'}],
    ]


def test_single_subscription_gets_one_renew_button_and_trial_only_gets_buy():
    single = REAL_CONTEXT.splitlines()[0]

    assert answer_buttons.renew_buttons(single) == [
        [{'text': 'Продлить подписку', 'url': f'{SITE}/subscriptions/57923/renew', 'style': 'success'}]]
    assert answer_buttons.renew_buttons(REAL_CONTEXT.splitlines()[2])[0][0]['text'] == 'Купить подписку'


@pytest.mark.parametrize(('question', 'expected'), [
    ('Продлить подписку', True),
    ('хочу продлить заранее', True),
    ('Оплатила, а подписка не продлилась', False),
    ('Деньги списали за продление', False),
    ('Как отключить автопродление?', False),
    ('Текущая подписка продлится на 3 месяца?', False),
    ('ссылка на продление моей подписки', True),
])
def test_renewal_request_is_a_plain_wish_without_payment_complaint(question, expected):
    from ai_support_bot.app.services.service_identity import renewal_request

    assert renewal_request(question) is expected


@pytest.mark.asyncio
async def test_payment_tools_do_not_set_flags_for_a_plain_renewal_wish():
    from ai_support_bot.app.services.dialogue_tools import execute_support_tool

    state = {'issue': 'other', '_renewal_request': True}
    result = await execute_support_tool('check_subscription_renewal', '{}', 1, state, 'Продлить подписку')

    assert 'error' in result
    assert state == {'issue': 'other', '_renewal_request': True}


def test_renew_answer_is_one_short_line_for_each_case():
    assert answer_buttons.renew_answer(answer_buttons.renew_buttons(REAL_CONTEXT)).startswith('Выберите подписку на кнопке ниже')
    assert answer_buttons.renew_answer(answer_buttons.renew_buttons(REAL_CONTEXT.splitlines()[0])).startswith('Продлить можно по кнопке ниже')
    assert answer_buttons.renew_answer(answer_buttons.renew_buttons(REAL_CONTEXT.splitlines()[2])) == 'Оформить подписку можно по кнопке ниже.'


def test_unreadable_subscriptions_get_no_buttons_rather_than_buy():
    assert answer_buttons.renew_buttons('Подписки:\n  • подписка №1 (id=11), статус=active, до 01.10.2026') == []
    assert answer_buttons.renew_buttons('') == []


def test_customer_without_subscriptions_gets_buy():
    assert answer_buttons.renew_buttons('Баланс: 0 ₽\nПодписки: отсутствуют')[0][0]['text'] == 'Купить подписку'
