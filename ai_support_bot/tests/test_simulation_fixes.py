"""Weak spots found by the 500-dialogue simulation (2026-11-19): the phrases are the simulated customers' own."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_support_bot.app.navigation.query import navigation_target
from ai_support_bot.app.services.agent import (
    WAITING_ANSWER,
    _SERVICE_REGION_BLOCK,
    _foreign_language,
    _waiting_after_handoff,
    looks_hedged,
)
from ai_support_bot.app.services.response_rules import (
    SHADOWROCKET_ANSWER,
    apps_answer,
    diagnostics_exhausted,
    guarantee_question,
    mandatory_handoff,
)
from ai_support_bot.app.services.support_policy import SUPPORT_POLICY
from ai_support_bot.tests.test_live_review_fixes import _ask, flow  # noqa: F401  (fixture)


def _history(*pairs):
    return [SimpleNamespace(role=role, content=content) for role, content in pairs]


HANDED_OVER = _history(('user', 'Не работает'), ('assistant', '🎧 Передаю обращение команде для проверки, подождите, пожалуйста.'))


@pytest.mark.parametrize('question', ['ок, жду', 'Хорошо, спасибо. Подожду 🙏', 'ок, спасибо, подожду', 'Ок, понял. Жду тогда.', 'thanks, will wait'])
def test_waiting_after_a_handoff_is_not_a_new_handoff(question):
    assert _waiting_after_handoff(question, HANDED_OVER)


@pytest.mark.parametrize(('question', 'history'), [
    ('ок, жду', _history(('assistant', 'Смените сервер в приложении.'))),
    ('А когда ответят?', HANDED_OVER),
    ('Вот ещё скрин, теперь пишет другую ошибку', HANDED_OVER),
])
def test_other_messages_after_a_handoff_still_count(question, history):
    assert not _waiting_after_handoff(question, history)


@pytest.mark.asyncio
async def test_waiting_after_a_money_handoff_is_not_handed_over_again(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    first = await _ask(agent_mod, Session, 'С карты списали 219 ₽ автопродлением, хотя мне не нужно. Верните, пожалуйста.')
    assert first['escalate'] is True
    second = await _ask(agent_mod, Session, 'Понял, спасибо. Тогда жду.')
    assert second['escalate'] is False
    assert WAITING_ANSWER in second['answer']


@pytest.mark.asyncio
async def test_waiting_reply_does_not_hand_over_again(flow, monkeypatch):  # noqa: F811
    captured, agent_mod, Session = flow
    captured['reply'] = 'Передаю обращение команде для проверки, подождите, пожалуйста.'
    first = await _ask(agent_mod, Session, 'Добрый день, хотел обсудить одну идею')
    assert first['escalate'] is True
    second = await _ask(agent_mod, Session, 'ок, жду')
    assert second['escalate'] is False
    assert WAITING_ANSWER in second['answer']


@pytest.mark.parametrize(('question', 'foreign'), [
    ('Hi! I’m looking for a VPN plan for my whole family — there are 6 of us.', True),
    ('Hello. Your french server locate me in USA.', True),
    ('Привет, не работает VPN', False),
    ('ok', False),
])
def test_foreign_language_is_detected(question, foreign):
    assert _foreign_language(question) is foreign


@pytest.mark.asyncio
async def test_english_question_gets_the_language_note_and_the_catalog(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    captured['reply'] = 'The Family plan fits.'
    await _ask(agent_mod, Session, 'Hi! Which plan is best for a family of 6 devices?')
    prompt = captured['prompts'][-1]
    assert 'Клиент пишет не по-русски' in prompt


@pytest.mark.parametrize('question', ['Хорошо, я сейчас так попробую.', 'Поняла, спасибо! Сейчас попробую оформить.'])
def test_acknowledgement_is_not_hedging(question):
    assert not looks_hedged('Обычно после этого всё работает.', question, 0)


@pytest.mark.parametrize(('answer', 'question'), [
    ('Скорее всего лимит устройств 3.', 'какой у меня лимит устройств'),
    ('Наверное оплата придёт в течение часа.', 'когда пройдёт оплата'),
])
def test_guessed_facts_still_count_as_hedging(answer, question):
    assert looks_hedged(answer, question, 0)


def test_catalog_backs_a_device_answer():
    assert not looks_hedged('Обычно можно подключить телефон и ноутбук.', '5 устройств — это телефон и ноут?', 1)


EXPIRED = 'Telegram ID: 4242\nБаланс: 0.00 ₽\nПодписки:\n  • подписка №1 (id=11), статус=expired, триал=нет, до 12.11.2026, устройств=5'


@pytest.mark.parametrize('question', [
    'перестал работать VPN в приложении SligVPN, соединение не устанавливается',
    'подключение есть, а сайты не открываются',
    'My VPN stopped working today',
])
@pytest.mark.asyncio
async def test_ended_subscription_is_recognised_in_any_wording(flow, monkeypatch, question):  # noqa: F811
    captured, agent_mod, Session = flow
    monkeypatch.setattr(agent_mod, 'build_user_context', AsyncMock(return_value=EXPIRED))
    captured['reply'] = 'Перезапустите приложение и смените сервер.'
    result = await _ask(agent_mod, Session, question)
    assert 'Подписка закончилась 12.11.2026' in result['answer']


@pytest.mark.parametrize('question', [
    'Есть ли у вас гарантия и вернут ли деньги, если сервис у меня не заработает?',
    'Подскажите пожалуйста а есть ли у вас гарантия на годовую подписку и вернут ли деньги если вдруг не заработает у меня.',
])
def test_guarantee_before_buying_is_answered_not_refunded(question):
    assert guarantee_question(question)
    assert mandatory_handoff(question, []) is None


@pytest.mark.parametrize('question', ['Купил годовую, не работает, верните деньги', 'Хочу вернуть деньги за подписку'])
def test_real_refund_still_goes_to_the_team(question):
    assert not guarantee_question(question)
    assert mandatory_handoff(question, []) == 'refund'


def test_shadowrocket_answer_is_plain_and_given_once():
    assert 'Shadowrocket с нашей подпиской не работает' in SHADOWROCKET_ANSWER
    first = apps_answer('В Shadowrocket пишет «не поддерживается»', [])
    assert first == SHADOWROCKET_ANSWER
    again = apps_answer('Я уже в Shadowrocket всё добавил, что делать?', _history(('assistant', SHADOWROCKET_ANSWER)))
    assert again != SHADOWROCKET_ANSWER


STEPS = _history(('user', 'Не работает VPN'), ('assistant', 'Смените сервер в приложении и обновите подписку в приложении.'))


@pytest.mark.parametrize('question', [
    'попробую щас, если не ок, напишу какой стоит и где не работает',
    'Хорошо, сейчас попробую и отпишусь',
    'Подключается, но сайты не открываются, а в Telegram не грузятся фото и видео',
])
def test_a_promise_or_a_longer_description_is_not_exhausted(question):
    assert not diagnostics_exhausted(question, STEPS)


@pytest.mark.parametrize('question', ['Не работает', 'не помогло', 'Сменил сервер, обновил подписку — всё так же'])
def test_failure_after_our_steps_is_exhausted(question):
    assert diagnostics_exhausted(question, STEPS)


@pytest.mark.parametrize('question', [
    'Пытаюсь подключиться через ваш VPN, но сервис пишет, что моя страна не поддерживается',
    'ChatGPT: unsupported country',
])
def test_a_service_region_block_is_not_the_connection_page(question):
    assert _SERVICE_REGION_BLOCK.search(question)
    assert navigation_target(question) is None


@pytest.mark.parametrize('question', [
    'ок, ща попробую', 'Понял, спасибо. Тогда продлю через кнопку.', 'Thanks, that’s clear now.',
    'Got it, thanks. That’s enough for now.', 'Спасибо вам\nЯ сейчас посмотрю\nЕсли что еще спрошу',
    'ок, щас переключу и перезапущу. если не поможет — напишу.',
])
def test_acknowledgements_are_recognised(question):
    from ai_support_bot.app.services.agent import _acknowledgement

    assert _acknowledgement(question)


@pytest.mark.parametrize('question', [
    'Понял, спасибо. А если не зайдёт, возврат реально рассматривают?', 'ок, попробовал — не помогло',
    'Спасибо, но всё равно ошибка', 'Почему так медленно?', 'Хочу продлить подписку',
])
def test_questions_and_complaints_are_not_acknowledgements(question):
    from ai_support_bot.app.services.agent import _acknowledgement

    assert not _acknowledgement(question)


@pytest.mark.asyncio
async def test_acknowledgement_gets_a_short_reply_without_a_handoff(flow):  # noqa: F811
    captured, agent_mod, Session = flow
    captured['reply'] = ''  # the model returns nothing to «ок, ща попробую»
    result = await _ask(agent_mod, Session, 'ок, ща попробую')
    assert result['escalate'] is False
    assert 'напишите сюда' in result['answer']


def test_policy_covers_the_simulation_findings():
    for text in ('отвечай на языке клиента', 'Подписка закончилась: если в данных аккаунта подписка истекла',
                 'до 5 — Стандарт, 6–10 — Семейный', '«Добавить в Happ»', '«Страна не поддерживается»',
                 'доставку писем ты проверить не можешь', 'Не видно сервера в списке',
                 'не передавай обращение повторно', 'Счёт на юрлицо'):
        assert text in SUPPORT_POLICY, text
    assert SUPPORT_POLICY.strip().splitlines()[-1].startswith('Отвечай кратко')
