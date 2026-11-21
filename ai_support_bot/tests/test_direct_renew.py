"""Direct renew page (21.11.2026): web.slig.app/api/cabinet/public/renew/<code from the subscription link>."""
from types import SimpleNamespace

from ai_support_bot.app.services import answer_buttons
from ai_support_bot.app.services.response_rules import blank_page_answer
from ai_support_bot.app.services.support_policy import SUPPORT_POLICY

PAGE = 'https://web.slig.app/api/cabinet/public/renew/'
PAID = ('Подписки:\n  • подписка №1 (id=101), статус=active, триал=нет, до 15.12.2026 (осталось 24 дн.), трафик=безлимит, '
        'устройств=5 (настроенный лимит, не количество подключённых), автоплатеж=выкл, ссылка=https://connect.sligvpn.ru/AbCd1234Efgh')
SECOND = ('\n  • подписка №2 (id=102), статус=active, триал=нет, до 03.01.2027, трафик=безлимит, устройств=10, '
          'автоплатеж=выкл, ссылка=https://connect.sligvpn.ru/XyZ987654321')
TRIAL = ('Подписки:\n  • подписка №1 (id=201), статус=active, триал=да, до 22.11.2026, трафик=10 ГБ, устройств=1, '
         'автоплатеж=выкл, ссылка=https://connect.sligvpn.ru/TrIaL12345')
NO_LINK = 'Подписки:\n  • подписка №1 (id=301), статус=active, триал=нет, до 15.12.2026, трафик=безлимит, устройств=5, автоплатеж=выкл'


def test_paid_subscription_renews_on_its_own_page():
    rows = answer_buttons.renew_buttons(PAID)
    assert rows == [[{'text': 'Продлить подписку', 'url': PAGE + 'AbCd1234Efgh', 'style': 'success'}]]
    text = answer_buttons.renew_answer(rows)
    assert 'оплата сразу на сайте, без бота' in text and 'кнопка «Продлить»' in text


def test_each_subscription_gets_its_own_page():
    rows = answer_buttons.renew_buttons(PAID + SECOND)
    assert [row[0]['url'] for row in rows] == [PAGE + 'AbCd1234Efgh', PAGE + 'XyZ987654321']


def test_trial_goes_to_the_page_that_offers_the_standard_plan():
    rows = answer_buttons.renew_buttons(TRIAL)
    assert rows[0][0]['url'] == PAGE + 'TrIaL12345'
    assert rows[0][0]['text'] == 'Купить подписку'


def test_without_a_link_the_cabinet_page_stays():
    rows = answer_buttons.renew_buttons(NO_LINK)
    assert rows[0][0]['url'] == 'https://web.slig.app/subscriptions/301/renew'
    assert 'без бота' not in answer_buttons.renew_answer(rows)


def test_model_renew_marker_uses_the_page_too():
    assert answer_buttons.build(['renew'], PAID)[0][0]['url'] == PAGE + 'AbCd1234Efgh'


def test_renew_page_named_in_an_answer_becomes_a_button():
    assert answer_buttons.page_actions(f'Оплатить можно тут: {PAGE}AbCd1234Efgh') == ['renew']


def test_blank_page_after_renew_mentions_the_app_button():
    history = [SimpleNamespace(role='assistant', content='⏳ Продлить можно по кнопке ниже — оплата сразу на сайте, без бота.')]
    answer = blank_page_answer('Не открывается. Белый экран', history)
    assert '«Продлить» в приложении Happ' in answer


def test_policy_knows_the_renew_page():
    assert 'кнопка «Продлить»' in SUPPORT_POLICY and 'без входа в кабинет' in SUPPORT_POLICY
    assert 'сам такую ссылку не составляй' in SUPPORT_POLICY
    assert SUPPORT_POLICY.strip().splitlines()[-1].startswith('Отвечай кратко')
