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


@pytest.mark.parametrize(('text', 'said'), [
    ('Спасибо, проверю. Сумма 500 ₽, время 14:20. Передам на проверку.', True),
    ('Проверю и передам обращение на ручную проверку.', True),
    ('Если после этих шагов не заработает, напишите — передам обращение команде.', False),
    ('Если не поможет — передам обращение на проверку.', False),
    ('I’ve passed it for review.', True),
    ('I’ve passed it to the team for review.', True),
    ('I’ll pass this for manual check.', True),
    ('If it still drops, I’ll pass this for manual check.', False),
])
def test_handoff_in_any_tense_and_in_english(text, said):
    from ai_support_bot.app.services.agent import _handoff_said

    assert _handoff_said(text) is said


@pytest.mark.parametrize('question', [
    'Hi, I saw a 20% discount for the yearly plan, but when I tried to pay, the price was full amount with no discount.',
    'My promo code doesn’t work at checkout',
])
def test_english_price_mismatch_goes_to_the_team(question):
    assert mandatory_handoff(question, []) == 'price_mismatch'


@pytest.mark.parametrize(('text', 'operation'), [
    ('Идентификатор операции: 487219365104', '487219365104'),
    ('а идентификатор операции у меня в выписке такой: 9482735612.', '9482735612'),
    ('идентификатор платежа: 7f3a9c2e-1b4d-4a6b-9f2e-8c1d4b6a7e11. Пополнял через сайт.', '7f3a9c2e-1b4d-4a6b-9f2e-8c1d4b6a7e11'),
    ('номер операции A1B2C3D4E5F6G7H8I9J0K', 'A1B2C3D4E5F6G7H8I9J0K'),
])
def test_payment_operation_numbers_are_captured(text, operation):
    from ai_support_bot.app.services.dialogue_tools import capture_payment_details

    state = {}
    capture_payment_details(text, state)
    assert state.get('payment_operation_id') == operation


def test_card_numbers_are_not_taken_for_an_operation():
    from ai_support_bot.app.services.dialogue_tools import capture_payment_details

    state = {}
    capture_payment_details('номер карты 4276123456789012, оплата 500 ₽', state)
    assert 'payment_operation_id' not in state


def test_relative_payment_time_is_captured():
    from ai_support_bot.app.services.dialogue_tools import capture_payment_details

    state = {}
    capture_payment_details('500 ₽, сегодня, примерно 10–15 минут назад', state)
    assert state.get('payment_amount') and state.get('payment_time')


@pytest.mark.parametrize('question', ['Хорошо, сейчас напишу. Идентификатор операции: 487219365104', 'Ок, вот почта ivan@mail.ru', 'Хорошо, 500 ₽ было'])
def test_messages_with_data_are_not_acknowledgements(question):
    from ai_support_bot.app.services.agent import _acknowledgement

    assert not _acknowledgement(question)


def test_policy_covers_the_simulation_findings():
    for text in ('отвечай на языке клиента', 'Подписка закончилась: если в данных аккаунта подписка истекла',
                 'до 5 — Стандарт, 6–10 — Семейный', '«Добавить в Happ»', '«Страна не поддерживается»',
                 'доставку писем ты проверить не можешь', 'Не видно сервера в списке',
                 'не передавай обращение повторно', 'Счёт на юрлицо'):
        assert text in SUPPORT_POLICY, text
    assert SUPPORT_POLICY.strip().splitlines()[-1].startswith('Отвечай кратко')
