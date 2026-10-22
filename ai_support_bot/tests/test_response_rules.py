from types import SimpleNamespace

import pytest

from ai_support_bot.app.navigation.query import contextual_navigation_query, navigation_target
from ai_support_bot.app.services.response_rules import compact_knowledge, is_payment_missing, mandatory_handoff
from ai_support_bot.app.services.telegram_html import sanitize_telegram_html


def test_long_channel_followup_uses_previous_user_topic():
    history = [
        SimpleNamespace(role='user', content='Как подключить VPN?'),
        SimpleNamespace(role='assistant', content='Баланс → Пополнить → Stars'),
        SimpleNamespace(role='user', content='Нужна инструкция именно для сайта'),
    ]
    query = contextual_navigation_query('А теперь покажи как в Telegram-боте', history)
    assert navigation_target(query) == 'subscription_connect'
    assert 'Stars' not in query


def test_context_budget_limits_seven_old_examples():
    knowledge = [{'content': 'x' * 1200, 'score': 0.9, 'question': str(i)} for i in range(7)]
    selected = compact_knowledge(knowledge)
    assert sum(len(item['content']) for item in selected) <= 2400
    assert len(selected) == 2


@pytest.mark.parametrize('question', ['Не подключайте оператора', 'Как продлить подписку?', 'VPN не работает', 'Какие условия реферальной программы?'])
def test_regular_questions_do_not_force_handoff(question):
    assert mandatory_handoff(question, []) is None


def test_telegram_html_closes_tags_and_escapes_text():
    assert sanitize_telegram_html('<b>Цена < 100 & 200') == '<b>Цена &lt; 100 &amp; 200</b>'


def test_telegram_html_preserves_safe_clickable_links():
    assert sanitize_telegram_html('<a href="https://web.slig.app/balance">Баланс</a>') == '<a href="https://web.slig.app/balance">Баланс</a>'


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://user:password@example.com', 'file:///etc/passwd'])
def test_telegram_html_removes_unsafe_links(url):
    assert sanitize_telegram_html(f'<a href="{url}">Текст</a>') == 'Текст'


@pytest.mark.parametrize('question', ['Почему не пополнился баланс?', 'Не пришли деньги', 'Деньги не появились'])
def test_missing_payment_variants_start_same_workflow(question):
    assert is_payment_missing(question)


def test_topup_without_renewal_is_not_a_missing_payment():
    assert not is_payment_missing('Пополнил баланс, а подписка не продлилась. Почему?')


def test_past_ticket_private_links_and_emails_do_not_reach_model():
    knowledge = compact_knowledge([{'content': 'Адрес other@example.com, ссылка https://private.example/secret-token', 'score': 0.9}])
    assert 'other@example.com' not in knowledge[0]['content']
    assert 'secret-token' not in knowledge[0]['content']
    assert 'скрыт' in knowledge[0]['content']


@pytest.mark.parametrize(('state', 'expected'), [
    ({'receipt_reported': True}, 'Где вы оплачивали'),
    ({'purchase_channel': 'site', 'receipt_reported': True}, 'регистрировались'),
    ({'purchase_channel': 'site', 'registration_method': 'email'}, 'адрес электронной почты'),
    ({'purchase_channel': 'telegram'}, 'Пришлите чек'),
])
def test_payment_intake_asks_only_next_missing_detail(state, expected):
    from ai_support_bot.app.services.response_rules import payment_next_step

    answer, escalate = payment_next_step(state)
    assert expected in answer
    assert answer.count('?') <= 1
    assert not escalate


def test_complete_payment_intake_hands_off_without_repeating_receipt_request():
    from ai_support_bot.app.services.response_rules import payment_next_step

    answer, escalate = payment_next_step({'purchase_channel': 'site', 'registration_method': 'email', 'email': 'buyer@example.com', 'receipt_reported': True})
    assert escalate
    assert 'Передаю' in answer
    assert 'Пришлите' not in answer


def test_https_url_in_code_becomes_plain_clickable_url():
    assert sanitize_telegram_html('<code>https://web.slig.app/balance?a=1&amp;b=2</code>') == 'https://web.slig.app/balance?a=1&amp;b=2'
    assert sanitize_telegram_html('<code> HTTPS://web.slig.app </code>') == 'HTTPS://web.slig.app'
    assert sanitize_telegram_html('<code>some-token</code>') == '<code>some-token</code>'


@pytest.mark.parametrize('question', [
    'Деньги списали за продление, но подписка не продлилась',
    'Не пополнил баланс, подписка не продлилась',
    'Пополнил баланс, а подписка не продлилась. Когда сработает автоплатёж?',
])
def test_personal_payment_questions_are_not_generic_renewal_guidance(question):
    from ai_support_bot.app.services.response_rules import is_topup_without_renewal

    assert not is_topup_without_renewal(question)


@pytest.mark.parametrize('question', ['27373', 'Что', 'Как улучшить качество', 'Я другой вопрос задал', 'Другой вопрос', 'А какая скорость?'])
def test_new_or_ambiguous_replies_never_inherit_old_navigation(question):
    history = [SimpleNamespace(role='user', content='Как активировать промокод')]
    assert contextual_navigation_query(question, history, 'Ранее обсуждали промокод') == question


def test_channel_followup_stops_at_intervening_new_topic():
    history = [
        SimpleNamespace(role='user', content='Как активировать промокод'),
        SimpleNamespace(role='user', content='Как улучшить качество'),
    ]
    assert contextual_navigation_query('А на сайте?', history) == 'А на сайте?'


@pytest.mark.parametrize('question', ['Возавтрат денег', 'Возврат денег', 'Возрат денег', 'Возврат', 'Хочу возврат денег'])
def test_refund_including_reported_typo_uses_refund_handoff(question):
    assert mandatory_handoff(question, []) == 'refund'


@pytest.mark.parametrize('question', ['Какой возраст сервиса?', 'Как активировать промокод?', 'Как вернуть ссылку подписки?'])
def test_refund_typo_matching_does_not_capture_other_questions(question):
    assert mandatory_handoff(question, []) is None


def test_connect_subscription_is_navigation_request():
    assert navigation_target('Как подключить подписку') == 'subscription_connect'


@pytest.mark.parametrize(('old', 'new'), [
    ('Как активировать промокод', 'Как улучшить качество VPN?'),
    ('VPN не работает на iPhone', 'Как купить подписку?'),
    ('Какие приложения для Android?', 'Как активировать промокод?'),
    ('Сколько устройств можно подключить?', 'Как привязать Telegram?'),
    ('Как купить подписку?', 'Не пришли деньги'),
    ('Как улучшить качество VPN?', 'Почему не активируется промокод?'),
    ('Хочу предложить сотрудничество', 'Возавтрат денег'),
])
def test_automatic_topic_switch_is_independent_of_payment_state(old, new):
    from ai_support_bot.app.services.response_rules import starts_new_topic

    history = [SimpleNamespace(role='user', content=old)]
    assert starts_new_topic(new, history, {'issue': 'other'})


@pytest.mark.parametrize(('old', 'followup'), [
    ('VPN не работает на iPhone', 'Перезапустил, не помогло'),
    ('Как подключить VPN?', 'Нужна инструкция именно для сайта'),
    ('Как подключить VPN?', 'А теперь покажи как в Telegram-боте'),
    ('Какие приложения скачать на Android?', 'А для iPhone?'),
    ('Как купить подписку?', 'Сколько стоит?'),
    ('Как активировать промокод?', 'Где это?'),
    ('Как удалить устройство?', 'Как удалить его?'),
    ('VPN не работает на iPhone', 'Лимит устройств — 3'),
    ('Как купить подписку?', 'да'),
])
def test_followups_keep_their_topic_without_payment_state(old, followup):
    from ai_support_bot.app.services.response_rules import starts_new_topic

    history = [SimpleNamespace(role='user', content=old)]
    assert not starts_new_topic(followup, history, {'issue': 'other'})


@pytest.mark.parametrize('text', [
    'Откройте https://t.me/SligVPN_bot',
    'Откройте https://t.me/SligVPN_bot/.',
    '<a href="https://t.me/SligVPN_bot">Бот</a>',
    '<code>https://t.me/SligVPN_bot</code>',
])
def test_support_bot_links_include_start(text):
    assert 'https://t.me/SligVPN_bot?start=ai_support' in sanitize_telegram_html(text)


@pytest.mark.parametrize('url', [
    'https://t.me/SligVPN_bot?start=ref_123',
    'https://t.me/SligVPN_bot?startapp=subscription',
    'https://t.me/SligVPN_bot/123',
    'https://t.me/SligVPN_bot_other',
    'https://t.me/SligVPN_bot.evil.example',
    'https://web.slig.app/subscriptions',
])
def test_start_normalization_preserves_existing_deep_links_and_other_urls(url):
    expected = f'<a href="{url}">https://t.me/SligVPN_bot</a>' if '?start' in url else url
    assert sanitize_telegram_html(url) == expected
    assert sanitize_telegram_html(f'<a href="{url}">Бот</a>') == f'<a href="{url}">Бот</a>'


@pytest.mark.parametrize('question', [
    'как обновить подписку',
    'я хочу не продлить, а обновить, вы же сказали мне обновить подписку выше',
    'Как обновить список серверов?',
])
def test_refresh_request_does_not_route_to_paid_renewal(question):
    assert navigation_target(question) is None


def test_explicit_renewal_still_routes_to_renewal():
    assert navigation_target('Как продлить подписку?') == 'subscription_extend'


@pytest.mark.parametrize('question', ['как', 'А как?', 'Как это сделать?', 'Не понял'])
def test_short_clarification_preserves_topic(question):
    from ai_support_bot.app.services.response_rules import starts_new_topic

    history = [
        SimpleNamespace(role='user', content='Не работают белые списки'),
        SimpleNamespace(role='assistant', content='Обновите подписку в VPN-приложении.'),
    ]
    assert not starts_new_topic(question, history, {})


@pytest.mark.parametrize('text', [
    'https://t.me/SligVPN_bot?start=ai_support',
    '<code>https://t.me/SligVPN_bot?start=ai_support</code>',
    '<a href="https://t.me/SligVPN_bot?start=ai_support">https://t.me/SligVPN_bot?start=ai_support</a>',
])
def test_bot_start_parameter_is_hidden_in_link_target(text):
    assert sanitize_telegram_html(text) == '<a href="https://t.me/SligVPN_bot?start=ai_support">https://t.me/SligVPN_bot</a>'


def test_bot_link_preserves_query_escaping_and_surrounding_punctuation():
    text = 'Бот (https://t.me/SligVPN_bot?start=ref_123&x=1).'
    assert sanitize_telegram_html(text) == 'Бот (<a href="https://t.me/SligVPN_bot?start=ref_123&amp;x=1">https://t.me/SligVPN_bot</a>).'
