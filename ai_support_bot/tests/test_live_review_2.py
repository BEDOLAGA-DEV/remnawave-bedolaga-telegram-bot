"""Second live review (2026-11-17 evening): the phrases are the customers' own."""
from unittest.mock import AsyncMock

import pytest

from ai_support_bot.app.navigation.query import navigation_target
from ai_support_bot.app.services.agent import _HANDOFF_SAID, _WORKS_SOMEWHERE, looks_hedged
from ai_support_bot.app.services.response_rules import mandatory_handoff, partnership_offer, price_mismatch
from ai_support_bot.app.services.service_identity import renewal_request
from ai_support_bot.tests.test_live_review_fixes import _ask, flow  # noqa: F401  (fixture)

AD_OFFER = (
    'Здравствуйте я только что увидел что вы рекламируете ваш впн и мне появилось желание предлагать мои услуги '
    'для увеличения трафика могу ваш баннер на мои акки поставить и оплата по просмотру цены не большие за '
    '1000просмотров если интересует напишите детально обсудим'
)


@pytest.mark.parametrize('question', [
    AD_OFFER,
    'Увидела ваш проект @SligApp и хочу предложить сотрудничество с нами. По какой ставке работаете? 3.5% на СБП',
    'Пишу с предложением о размещении рекламы в онлайн кинотеатре. Если вам актуальна закупка трафика — подскажу по ценам',
    'Могу разместить ваш баннер в своём канале, CPM 300 ₽',
])
def test_advertising_offers_go_to_the_team(question):
    assert partnership_offer(question)
    assert mandatory_handoff(question, []) == 'partnership'


@pytest.mark.parametrize('question', [
    'Какие услуги вы предлагаете?',
    'Сколько стоит подписка на месяц?',
    'Что вы можете предложить по тарифам?',
    'Как работает реферальная программа и какие условия реферальной ссылки?',
])
def test_customer_questions_are_not_partnership(question):
    assert not partnership_offer(question)


@pytest.mark.parametrize('question', [
    'по скидке за год должно выходить 1228 р, а при оплате 1536?',
    'Хочу оплатить со скидкой. Когда выбираю срок то пишет цену со скидкой. Как нажимаю заплатить цена появляется без скидки',
    'Скидка 36% показывается, а при оплате 1536 ₽',
])
def test_price_differs_at_checkout_goes_to_the_team(question):
    assert price_mismatch(question)
    assert mandatory_handoff(question, []) == 'price_mismatch'


@pytest.mark.parametrize('question', ['Есть скидка при оплате за год?', 'Сколько стоит год со скидкой?', 'Какие есть скидки?'])
def test_discount_questions_stay_with_the_agent(question):
    assert not price_mismatch(question)


def test_already_bought_subscription_leads_to_connecting_it():
    question = 'здравствуйте, увидела в максе приложение , купила подписку , но не знаю как активировать ключ , что делать ?'
    assert navigation_target(question) == 'subscription_connect'
    assert navigation_target('Как купить подписку?') == 'subs_buy_subscription'


@pytest.mark.parametrize('question', [
    'Не проходит оплата за продление',
    'Не получается продлить, ошибка оплаты',
    'При продлении платёж отклонён',
])
def test_failed_renewal_payment_is_not_a_renewal_wish(question):
    assert not renewal_request(question)


@pytest.mark.parametrize('question', ['Хочу продлить подписку', 'Продлите, пожалуйста', 'Как оплатить продление?'])
def test_plain_renewal_wish_still_counts(question):
    assert renewal_request(question)


@pytest.mark.parametrize(('evidence', 'claimed'), [
    ('Есть скидка при оплате за год?', False),
    ('Какая цена при оплате картой?', False),
    ('Оплатил со скидкой, а подписка не продлилась', True),
    ('Не проходит оплата за продление', True),
    ('Оплатил тариф на месяц, а показывает три дня', True),
])
def test_price_questions_are_not_payment_claims(evidence, claimed):
    from ai_support_bot.app.services.agent import _payment_claimed

    assert _payment_claimed(evidence) is claimed


def test_guess_words_in_vpn_diagnostics_are_not_hedging():
    wifi = 'здравствуйте, у меня работает впн только когда телефон подключен к какой-то сети вайфай, а просто он не работает, в чем дело?'
    assert not looks_hedged('Возможно, оператор пускает только сайты из белого списка.', wifi, 0)
    assert looks_hedged('Возможно, 219 ₽.', 'Сколько стоит подписка?', 0)


@pytest.mark.parametrize(('text', 'said'), [
    ('Спасибо за предложение о сотрудничестве! Передаю его команде, пожалуйста, подождите.', True),
    ('Передаю обращение на проверку.', True),
    ('Если не поможет — передам обращение на проверку.', False),
    ('Если хотите, передам ваш вопрос команде.', False),
])
def test_handoff_phrase_is_present_tense_only(text, said):
    assert bool(_HANDOFF_SAID.search(text)) is said


@pytest.mark.parametrize(('text', 'works'), [
    ('Скажите пожалуйста, работает только с вай фай', True),
    ('у меня работает впн только когда телефон подключен к вайфай', True),
    ('VPN не работает с мобильным интернетом', False),
    ('не работает впн только на мобильном', False),
    ('Впн не работает', False),
])
def test_vpn_working_somewhere_is_recognised(text, works):
    assert bool(_WORKS_SOMEWHERE.search(text)) is works


@pytest.mark.asyncio
async def test_model_saying_it_hands_over_reaches_the_team(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    captured['reply'] = 'Спасибо за идею! Передаю его команде, пожалуйста, подождите.'
    result = await _ask(agent_mod, Session, 'Добрый день, хотел обсудить одну идею')
    assert result['escalate'] is True
    assert not result.get('buttons')


@pytest.mark.asyncio
async def test_advertising_offer_gets_no_buy_button(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    result = await _ask(agent_mod, Session, AD_OFFER)
    assert result['escalate'] is True
    assert not result.get('buttons')
    assert 'сотрудничеств' in result['answer']


EXPIRED_TRIAL = (
    'Telegram ID: 4242\n'
    'Баланс: 0.00 ₽\n'
    'Подписки:\n'
    '  • подписка №1 (id=11), статус=expired, триал=да, до 10.10.2026, устройств=1'
)


@pytest.mark.asyncio
async def test_expired_template_skipped_when_vpn_works_on_wifi(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=EXPIRED_TRIAL))
    captured['reply'] = 'Переключитесь на сервер «Белые списки» и обновите подписку в приложении.'
    works = await _ask(agent_mod, Session, 'Скажите пожалуйста, работает только с вай фай. С мобильным интернетом не работает')
    assert 'закончился' not in works['answer']


@pytest.mark.asyncio
async def test_expired_template_still_answers_a_plain_failure(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=EXPIRED_TRIAL))
    captured['reply'] = 'Перезапустите приложение.'
    broken = await _ask(agent_mod, Session, 'Впн не работает')
    assert 'Пробный период закончился 10.10.2026' in broken['answer']
