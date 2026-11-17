"""Fixes found by reviewing live dialogues (2026-11-16/17): the phrases are the customers' own."""
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.core.config import ESCALATION_MARKER
from ai_support_bot.app.services.dialogue_tools import SUPPORT_TOOLS
from ai_support_bot.app.services.response_rules import (
    APP_STORE_REGION_HELP,
    CLIENTS,
    SHADOWROCKET_ANSWER,
    apps_answer,
    blank_page_answer,
    devices_exceeded,
    mandatory_handoff,
    money_dispute_topic,
    neutral_self_reference,
    region_change_refused,
    unwanted_renewal,
)
from ai_support_bot.app.services.support_policy import SUPPORT_POLICY
from ai_support_bot.app.services.telegram_html import is_allowed_link, sanitize_telegram_html


USER_CONTEXT = (
    'Telegram ID: 4242\n'
    'Баланс: 0.00 ₽\n'
    'Подписки:\n'
    '  • подписка №1 (id=11), статус=active, триал=нет, до 01.12.2026, устройств=5'
)


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

    captured: dict = {'prompts': [], 'reply': 'Готово.'}

    async def fake_chat_completion(messages, model, max_tokens, temperature, **kwargs):
        captured['prompts'].append(messages[0]['content'])
        result = {'content': captured['reply'], 'model': model, 'tokens_prompt': 1, 'tokens_completion': 1}
        if captured.get('responses'):
            result.update(captured['responses'].pop(0))
        return result

    monkeypatch.setattr(agent_mod.openai_client, 'chat_completion', fake_chat_completion)
    monkeypatch.setattr(agent_mod.rag_service, 'retrieve', AsyncMock(return_value=[]))
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


def _tool_reply(name):
    return {'content': None, 'tool_calls': [{'id': f'call-{name}', 'type': 'function',
                                            'function': {'name': name, 'arguments': '{}'}}]}


def _history(*pairs):
    return [SimpleNamespace(role=role, content=content) for role, content in pairs]


INCY_DIALOGUE = _history(
    ('user', 'На айфоне не могу найти Incy'),
    ('assistant', APP_STORE_REGION_HELP),
)


# 3. Apps: Happ and Incy first, the region instructions once, no Shadowrocket.

def test_shadowrocket_is_never_recommended():
    assert all('Shadowrocket' not in apps for apps in CLIENTS.values())
    assert CLIENTS['iPhone/iPad'] == ('Incy', 'Happ', 'Streisand')
    assert 'Shadowrocket' not in apps_answer('Какие приложения для iPhone?', [])
    assert 'iPhone/iPad — Incy, Happ, Streisand;' in SUPPORT_POLICY
    for line in SUPPORT_POLICY.splitlines():
        for sentence in re.split(r'(?<=[.!?])\s+', line):
            if 'Shadowrocket' in sentence:
                assert 'не рекомендуй' in sentence


@pytest.mark.parametrize('question', [
    'В Shadowrocket пишет «не поддерживается»',
    'Shadowrocket не работает с вашей подпиской',
    'Можно через Shadowrocket?',
])
def test_shadowrocket_complaint_gets_happ_or_incy_without_the_reason(question):
    answer = apps_answer(question, [])
    assert answer == SHADOWROCKET_ANSWER
    assert 'Happ' in answer and 'Incy' in answer and 'Streisand' in answer
    assert not re.search(r'hwid|лимит|устройств', answer, re.IGNORECASE)


@pytest.mark.parametrize('question', [
    # The live case: after the Incy talk «нет впн» got the whole region instructions a second time.
    'Телеграм не работает, потому что нет впн',
    'Приложение не подключается, нет интернета',
    'Нет выбора страны',
])
def test_a_plain_no_after_the_incy_talk_is_not_the_store_case(question):
    assert apps_answer(question, INCY_DIALOGUE) is None
    assert apps_answer(question, _history(('user', 'Поставил Incy на iPhone'), ('assistant', 'Хорошо.'))) is None


@pytest.mark.parametrize('question', [
    'В App Store нет Happ, как установить?',
    'Для скачивания в апп стор нет ни incy, ни happ.',
    'Incy нету в апсторе',
    'На iPhone не получается скачать Happ',
])
def test_missing_app_still_gets_the_region_instructions_once(question):
    assert apps_answer(question, []) == APP_STORE_REGION_HELP
    second = apps_answer(question, INCY_DIALOGUE)
    assert second != APP_STORE_REGION_HELP
    assert 'Streisand' in second and 'без смены региона' in second


@pytest.mark.parametrize(('question', 'history'), [
    ('Не хочу менять регион', []),
    ('Не могу сменить регион, у меня семейный доступ', []),
    ('На балансе Apple ID остались деньги, регион не меняется', []),
    ('У меня подписки Apple, регион не поменять', []),
    ('Не хочу ничего менять', INCY_DIALOGUE),
    ('Не получается сменить страну', INCY_DIALOGUE),
])
def test_region_refusal_gets_the_other_clients(question, history):
    assert region_change_refused(question, history)
    answer = apps_answer(question, history)
    assert 'Streisand' in answer
    assert APP_STORE_REGION_HELP not in answer


@pytest.mark.parametrize('question', ['Не могу сменить страну в приложении', 'Не хочу менять сервер', 'Как сменить регион?'])
def test_server_or_plain_region_questions_are_not_a_refusal(question):
    assert not region_change_refused(question, [])


def test_region_tool_is_only_for_a_missing_app():
    tool = next(item['function'] for item in SUPPORT_TOOLS if item['function']['name'] == 'app_store_region_help')
    assert 'любые формулировки' not in tool['description']
    assert 'Apple ID' in tool['description']
    assert 'Нет выбора страны/серверов' in tool['description']
    assert '«Нет выбора страны/серверов» в приложении — это список серверов в VPN-клиенте' in SUPPORT_POLICY
    assert 'не хочет менять регион' in SUPPORT_POLICY


DOWNLOAD_LINKS = [
    'https://apps.apple.com/us/app/happ-proxy-utility/id6504287215',
    'https://apps.apple.com/us/app/incy/id6756943388',
    'https://play.google.com/store/apps/details?id=llc.itdev.incy',
    'https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk',
    'https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe',
    'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-windows-setup.exe',
    'https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg',
    'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-macos-arm64.dmg',
    'https://github.com/INCY-DEV/incy-platforms/releases/latest/download/incy-macos-intel.dmg',
]


def test_policy_has_the_direct_download_links_and_they_survive_the_filter():
    for url in DOWNLOAD_LINKS:
        assert url in SUPPORT_POLICY
    for url in re.findall(r'https://[^\s,;)]+', SUPPORT_POLICY):
        url = url.rstrip('.')
        assert is_allowed_link(url), url
        assert '[ссылка удалена]' not in sanitize_telegram_html(f'Скачайте: {url}')
    # The owner's guides are allowed, but not offered to the model yet.
    assert 'happ-sub.at' not in SUPPORT_POLICY and 'incy.at' not in SUPPORT_POLICY


@pytest.mark.parametrize('url', DOWNLOAD_LINKS + [
    'https://happ.su/', 'https://incy.cc/', 'https://docs.incy.cc/ios', 'https://happ-sub.at/', 'https://incy.at/android',
    'https://github.com/happ-proxy/happ-android/releases', 'https://GITHUB.com/incy-dev/incy-platforms',
    'github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk',
])
def test_store_and_release_links_are_allowed(url):
    assert is_allowed_link(url)
    assert sanitize_telegram_html(f'Ссылка: {url}') == f'Ссылка: {url}'


@pytest.mark.parametrize('url', [
    'https://github.com/evil/happ-android/releases/latest/download/Happ.apk',
    'https://github.com/Happ-proxy-fake/happ-android',
    'https://github.com/Happ-proxy',
    'https://github.com/',
    'https://github.com/Happ-proxy/../evil/repo',
    'https://github.com/Happ-proxy/%2e%2e/evil/repo',
    'https://gist.github.com/Happ-proxy/abc',
    'https://raw.githubusercontent.com/Happ-proxy/happ-android/main/x.apk',
    'https://evilgithub.com/Happ-proxy/x',
    'https://incy.at.evil.example/x',
    'https://happ-sub.at@evil.example/',
    'https://t.me/SligVPN_bot/../evil_bot',
])
def test_other_github_paths_and_lookalikes_are_removed(url):
    assert not is_allowed_link(url)
    assert sanitize_telegram_html(f'Ссылка: {url}') == 'Ссылка: [ссылка удалена]'


@pytest.mark.asyncio
async def test_second_store_complaint_gets_alternatives_not_a_copy(flow):
    captured, agent_mod, Session = flow
    first = await _ask(agent_mod, Session, 'В App Store нет Happ, как установить?')
    assert '7️⃣' in first['answer']
    second = await _ask(agent_mod, Session, 'Всё равно нет Happ в App Store')
    assert '7️⃣' not in second['answer'] and 'Страна/регион' not in second['answer']
    assert 'Streisand' in second['answer']
    assert not second['escalate']
    assert not captured['prompts']


@pytest.mark.asyncio
async def test_no_vpn_after_the_incy_talk_goes_to_the_model(flow):
    captured, agent_mod, Session = flow
    await _ask(agent_mod, Session, 'В App Store нет Incy, как установить на айфон?')
    captured['reply'] = 'Проверьте, включён ли VPN в Incy, и выберите другой сервер.'
    result = await _ask(agent_mod, Session, 'Телеграм не работает, потому что нет впн')
    assert captured['prompts']
    assert 'Страна/регион' not in result['answer']
    assert 'другой сервер' in result['answer']


@pytest.mark.asyncio
async def test_region_tool_called_again_gives_alternatives(flow):
    captured, agent_mod, Session = flow
    captured['responses'] = [_tool_reply('app_store_region_help'), {'content': 'Смените регион.'}]
    first = await _ask(agent_mod, Session, 'Не ставится приложение на айфон, что делать')
    assert '7️⃣' in first['answer']
    captured['responses'] = [_tool_reply('app_store_region_help'), {'content': 'Смените регион.'}]
    second = await _ask(agent_mod, Session, 'Всё равно не ставится')
    assert '7️⃣' not in second['answer']
    assert 'Streisand' in second['answer']


# 4. «Белый экран» after a button is a page that did not load, not a VPN failure.

RENEW_REPLY = _history(('user', 'Хочу продлить подписку'),
                       ('assistant', '⏳ Продлить можно по кнопке ниже — в том числе заранее, пока подписка активна.'))


@pytest.mark.parametrize('question', ['Не открывается / Белый экран', 'Не открывается', 'Кнопка не работает', 'Пустая страница'])
def test_blank_page_after_a_button_points_to_the_browser_and_the_bot(question):
    answer = blank_page_answer(question, RENEW_REPLY)
    assert 'https://web.slig.app' in answer and 'Safari или Chrome' in answer
    assert 'через Telegram или по email' in answer
    assert 'https://t.me/SligVPN_bot: Мои подписки → выберите подписку → Продлить' in answer
    assert not re.search(r'сервер|перезапуст', answer)


@pytest.mark.parametrize(('question', 'history'), [
    ('Не открывается телеграм', RENEW_REPLY),
    ('Сайты не открываются', RENEW_REPLY),
    ('Не открывается', []),
    ('Не открывается', _history(('user', 'VPN не работает'), ('assistant', 'Смените сервер в приложении.'))),
    ('В Happ белый экран', RENEW_REPLY),
    ('В телеграме белый экран', []),
])
def test_ordinary_vpn_failures_keep_their_diagnostics(question, history):
    assert blank_page_answer(question, history) is None


def test_blank_page_rule_is_in_the_policy():
    assert 'Белый экран: если после кнопки или ссылки на кабинет' in SUPPORT_POLICY
    # A bare «белый экран» with no VPN words is the page too; only «→ Продлить» needs the renew context.
    assert 'выберите подписку.' in blank_page_answer('Белый экран', [])


@pytest.mark.asyncio
async def test_white_screen_after_the_renew_button_is_not_vpn_advice(flow):
    captured, agent_mod, Session = flow
    renew = await _ask(agent_mod, Session, 'Хочу продлить подписку')
    assert renew['buttons'] and 'renew' in renew['buttons'][0][0]['url']
    captured['prompts'].clear()
    captured['reply'] = 'Перезапустите приложение и смените сервер.'
    result = await _ask(agent_mod, Session, 'Не открывается / Белый экран')
    assert not captured['prompts']
    assert not result['escalate']
    assert 'Safari или Chrome' in result['answer']
    assert 'сервер' not in result['answer']
    captured['reply'] = 'Смените сервер в приложении.'
    vpn = await _ask(agent_mod, Session, 'Не открывается телеграм')
    assert captured['prompts']
    assert 'Safari' not in vpn['answer']


# 5. «Превышено число устройств» and a renewal nobody wanted.

LIVE_DEVICES_AND_RENEWAL = ('В Incy пишет «Макс. число устройств превышено», у меня 5 устройств в тарифе. '
                            'И ещё 10₽ мне на почту пришло автопродление хотя мне не нужно')


@pytest.mark.parametrize('question', [
    LIVE_DEVICES_AND_RENEWAL,
    'Пришло автопродление, хотя мне не нужно',
    'Автопродление сработало, хотя я его не включал, пришло 10₽',
    'Деньги списали за продление, хотя я не просил продлевать',
])
def test_unwanted_renewal_goes_to_the_team(question):
    assert unwanted_renewal(question)
    assert mandatory_handoff(question, []) == 'unwanted_renewal'
    assert money_dispute_topic(question)


@pytest.mark.parametrize('question', [
    'Мне не нужно автопродление, как отключить?',
    'Как отключить автопродление?',
    'Не могу отключить автопродление',
    'Автопродление не нужно, чтобы не списали — где выключить?',
    'Хочу продлить подписку',
])
def test_autopay_questions_without_a_charge_stay_self_service(question):
    assert not unwanted_renewal(question)
    assert mandatory_handoff(question, []) != 'unwanted_renewal'


@pytest.mark.parametrize(('question', 'expected'), [
    ('Макс. число устройств превышено', True),
    ('в приложении пишет что максимальное число устройств', True),
    ('Превышено количество устройств', True),
    ('Лимит устройств превышен', True),
    ('Сколько устройств можно подключить?', False),
    ('Какой лимит устройств?', False),
])
def test_device_limit_error_is_recognised(question, expected):
    assert devices_exceeded(question) is expected


def test_device_limit_rule_is_one_clear_answer():
    rule = next(line for line in SUPPORT_POLICY.splitlines() if line.startswith('Ошибка «превышено/макс. число устройств»'))
    assert 'все места подписки заняты' in rule
    assert 'ГЛАВНОЕ МЕНЮ → Мои подписки → выберите подписку → Устройства → Управление устройствами' in rule
    assert 'https://web.slig.app/subscriptions' in rule
    assert 'никогда не пиши «удалять не нужно»' in rule
    assert 'передай команде' in rule
    assert 'сбрасывать все устройства' in SUPPORT_POLICY
    assert 'Нежеланное автопродление' in SUPPORT_POLICY and 'чек не проси' in SUPPORT_POLICY


@pytest.mark.asyncio
async def test_live_devices_and_unwanted_renewal_dialogue(flow):
    captured, agent_mod, Session = flow
    captured['reply'] = 'Пришлите чек этой операции.'
    result = await _ask(agent_mod, Session, LIVE_DEVICES_AND_RENEWAL)
    assert not captured['prompts']
    assert result['escalate']
    assert result['escalation_reason'] == 'unwanted_renewal'
    assert 'чек' not in result['answer']
    assert 'Возврат пока не подтверждён' in result['answer']
    assert 'Автоплатеж → Выключить' in result['answer'] and '«Автопродление»' in result['answer']
    assert 'Управление устройствами' in result['answer'] and 'Мои устройства' in result['answer']
    assert 'удалять не нужно' not in result['answer']
    assert result['buttons'] == []
    assert 'tg-emoji' not in result['answer']
    follow = await _ask(agent_mod, Session, '?')
    assert follow['escalation_reason'] == 'unwanted_renewal'
    assert not captured['prompts']


# 6. «Отлично, рад, что заработало» kept the gendered «рад».

@pytest.mark.parametrize(('text', 'expected'), [
    ('✅ Отлично, рад, что заработало.', '✅ Отлично, хорошо, что заработало.'),
    ('Отлично, рада, что всё работает!', 'Отлично, хорошо, что всё работает!'),
    ('Супер, очень рад, что помогло.', 'Супер, хорошо, что помогло.'),
])
def test_glad_after_a_comma_is_neutralised(text, expected):
    assert neutral_self_reference(text) == expected


@pytest.mark.parametrize('text', [
    'Включите радио, что-то слышно?',
    'Мы рады, что вы с нами.',
    'Отлично, рады видеть вас снова.',
    'Отлично, радиус покрытия большой.',
    'Отлично, радостно, что заработало.',
])
def test_other_glad_words_are_left_alone(text):
    assert neutral_self_reference(text) == text


# 7. The owner's facts.

def test_policy_has_partner_and_guarantee_facts_and_keeps_the_closing_line_last():
    assert 'Партнёрская программа: заявки рассматриваем в течение 3 часов в будни, в выходные — дольше.' in SUPPORT_POLICY
    guarantee = next(line for line in SUPPORT_POLICY.splitlines() if line.startswith('Гарантия:'))
    assert 'да, гарантия есть' in guarantee and 'годовую подписку' in guarantee
    assert 'https://web.slig.app/offer' in guarantee and is_allowed_link('https://web.slig.app/offer')
    assert 'Не обещай автоматический возврат' in guarantee
    assert SUPPORT_POLICY.strip().splitlines()[-1].startswith('Отвечай кратко')


@pytest.mark.parametrize('question', ['Есть ли гарантия?', 'А на годовую подписку есть гарантия?'])
def test_guarantee_question_is_answered_not_handed_over(question):
    assert mandatory_handoff(question, []) is None
