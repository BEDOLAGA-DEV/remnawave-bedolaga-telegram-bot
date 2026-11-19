"""Third live review (2026-11-18/19) and the owner's answers: the phrases are the customers' own."""
from unittest.mock import AsyncMock

import pytest

from ai_support_bot.app.services.agent import _SUBSCRIPTION_CLAIM, _TECH_ISSUE
from ai_support_bot.app.services.response_rules import NOTICE, OUTAGE_STEP, handoff_answer, mandatory_handoff, price_mismatch
from ai_support_bot.app.services.service_identity import classify_service_source, renewal_verification_answer
from ai_support_bot.app.services.support_policy import SUPPORT_POLICY
from ai_support_bot.app.services.telegram_html import drop_removed_link_lines
from ai_support_bot.tests.test_live_review_fixes import _ask, flow  # noqa: F401  (fixture)


@pytest.mark.parametrize(('name', 'url'), [
    ('Strelka_vpn_bot', 'https://t.me/Strelka_vpn_bot'),
    ('ООО "Максим"', ''),
])
def test_our_gateway_names_on_a_receipt_are_ours(name, url):
    assert classify_service_source(name, url, 'payment_confirmation')['status'] == 'ours'


def test_gateway_bot_named_in_the_text_counts_as_ours():
    from ai_support_bot.app.services.service_identity import official_service_reference

    source = official_service_reference('Оплатила подписку через бота Strelka_vpn_bot, а подписка не продлилась')
    assert source and source['status'] == 'ours'


def test_unfamiliar_seller_on_a_receipt_goes_to_the_team():
    source = classify_service_source('SomeOther VPN', 'https://t.me/someother_vpn_bot', 'payment_confirmation')
    assert source['status'] == 'unknown'
    answer, escalate = renewal_verification_answer(source, {})
    assert escalate is True and 'Передаю чек команде' in answer


def test_policy_no_longer_sends_receipts_to_another_seller():
    assert 'Strelka_vpn_bot' in SUPPORT_POLICY
    assert 'предложи обратиться к продавцу из подтверждения' not in SUPPORT_POLICY


@pytest.mark.parametrize(('evidence', 'tech'), [
    ('очень маленькая скорость или ее нет вообще / подписку обновлял / комп перезагружал', True),
    ('плохо работает vpn на компьютере', True),
    ('Оплатил, вот чек', False),
    ('Здравствуйте, я оплатил впн но на Балансе нет ничего', False),
])
def test_screenshot_with_a_vpn_complaint_does_not_open_payment_checks(evidence, tech):
    assert bool(_TECH_ISSUE.search(evidence)) is tech


@pytest.mark.parametrize('question', [
    'У меня был промокод на скидку а сегодня не удается оплатить с ней почему то. Что делать?',
    'Промокод не применяется при оплате',
])
def test_promo_code_failing_at_checkout_goes_to_the_team(question):
    assert price_mismatch(question)
    assert mandatory_handoff(question, []) == 'price_mismatch'


@pytest.mark.parametrize('question', ['Промокод из колеса не работает', 'Как применить промокод?', 'Где ввести промокод?'])
def test_other_promo_questions_stay_with_the_agent(question):
    assert not price_mismatch(question)


@pytest.mark.parametrize('question', ['Уже несколько дней не работает VPN', 'Уже 4 или пятый день не работает телеграмм'])
def test_multi_day_outage_handoff_carries_one_quick_step(question):
    assert handoff_answer('diagnostics_failed', question) == NOTICE + '\n\n' + OUTAGE_STEP
    assert '«Белые списки»' in OUTAGE_STEP


def test_handoff_after_tried_steps_has_no_extra_advice():
    assert handoff_answer('diagnostics_failed', 'Неделю не работает, уже всё пробовал') == NOTICE


@pytest.mark.parametrize(('text', 'expected'), [
    ('Подписка у вас сейчас отсутствует.\nСайт на скриншоте: [ссылка удалена]', 'Подписка у вас сейчас отсутствует.'),
    ('[ссылка удалена]', ''),
    ('Зайдите на [ссылка удалена] и оплатите', 'Зайдите на [ссылка удалена] и оплатите'),
    ('Без ссылок', 'Без ссылок'),
])
def test_lines_left_with_only_a_removed_link_are_dropped(text, expected):
    assert drop_removed_link_lines(text) == expected


@pytest.mark.parametrize(('question', 'claim'), [
    ('Но я же пользуюсь уже оплаченным периодом', True),
    ('У меня есть подписка, но не работает', True),
    ('Пользуюсь вашим сервисом. Хотел спросить про сервера', False),
    ('Как продлить подписку?', False),
])
def test_paid_period_claim_is_recognised(question, claim):
    assert bool(_SUBSCRIPTION_CLAIM.search(question)) is claim


NO_SUBSCRIPTIONS = 'Telegram ID: 4242\nБаланс: 0.00 ₽\nПодписки: отсутствуют'


@pytest.mark.asyncio
async def test_paid_period_without_a_subscription_in_this_account_goes_to_the_team(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=NO_SUBSCRIPTIONS))
    captured['reply'] = 'Пришлите чек с суммой, временем и номером операции.'
    result = await _ask(agent_mod, Session, 'Но я же пользуюсь уже оплаченным периодом')
    assert result['escalate'] is True
    assert result['escalation_reason'] == 'account_mismatch'
    assert 'другой аккаунт' in result['answer'] and 'чек' not in result['answer']
    assert not result.get('buttons')


def test_policy_knows_slig_devices_and_promo_codes():
    assert '«Слиг»' in SUPPORT_POLICY
    assert 'Добавить устройство — это подключить его в свободное место подписки' in SUPPORT_POLICY
    assert 'проверить промокод ты не можешь' in SUPPORT_POLICY
    assert SUPPORT_POLICY.strip().splitlines()[-1].startswith('Отвечай кратко')
