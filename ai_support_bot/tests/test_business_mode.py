import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, GetBusinessConnection, ReadBusinessMessage, SendChatAction, SendMessage
from aiogram.types import BusinessConnection, CallbackQuery, Message, Update


os.environ.setdefault('AISUP_DATABASE_URL', 'sqlite+aiosqlite:///./data/ai_support_test.db')
os.environ.setdefault('AISUP_MAIN_DATABASE_URL', '')
os.environ.setdefault('AISUP_INCLUDE_REMNAWAVE_DATA', 'false')

from ai_support_bot.app.bot import bot as bot_mod
from ai_support_bot.app.db.models import BusinessChatControl
from ai_support_bot.app.services.openai_client import OpenAIError


@pytest.fixture
def service(monkeypatch):
    bot_mod._processed_updates.clear()
    monkeypatch.setattr(bot_mod, '_message_queue', bot_mod.MessageQueue(quiet_seconds=0, max_wait=0))
    bot = Bot('123456:TEST_TOKEN')
    connection = BusinessConnection(
        id='business-1',
        user={'id': 900, 'is_bot': False, 'first_name': 'Owner'},
        user_chat_id=900,
        date=datetime.now(UTC),
        is_enabled=True,
        rights={'can_reply': True, 'can_read_messages': True},
    )
    requests = []
    controls = {}

    async def get_chat(connection_id, chat_id, owner_id):
        key = (connection_id, chat_id)
        if key not in controls:
            controls[key] = BusinessChatControl(
                id=len(controls) + 1,
                connection_id=connection_id,
                chat_id=chat_id,
                owner_id=owner_id,
                manual_pause=False,
                pause_until=None,
                revision=0,
            )
        return controls[key]

    async def get_by_id(control_id):
        return next((c for c in controls.values() if c.id == control_id), None)

    async def active(control_id, revision):
        c = await get_by_id(control_id)
        return c is not None and c.revision == revision and not bot_mod.business_control.paused(c)

    async def set_manual(control_id, take):
        c = await get_by_id(control_id)
        c.manual_pause = take
        c.pause_until = None
        c.revision += 1

    async def operator_message(control_id, sent_at):

        c = await get_by_id(control_id)
        c.pause_until = sent_at + timedelta(minutes=30)
        c.revision += 1

    for name, replacement in dict(
        get_chat=get_chat, get_by_id=get_by_id, active=active, set_manual=set_manual, operator_message=operator_message
    ).items():
        monkeypatch.setattr(bot_mod.business_control, name, replacement)

    async def request(bot, method, **kwargs):
        requests.append(method)
        if isinstance(method, GetBusinessConnection):
            return connection
        if isinstance(method, (SendChatAction, ReadBusinessMessage, AnswerCallbackQuery)):
            return True
        return Message(
            message_id=100,
            date=datetime.now(UTC),
            chat={'id': method.chat_id, 'type': 'private'},
            text=method.text,
        )

    monkeypatch.setattr(bot.session, 'make_request', request)

    @asynccontextmanager
    async def session():
        yield None

    async def generate(*args, **kwargs):
        await asyncio.sleep(0)  # Let the typing task send its action.
        return {'answer': 'Ответ поддержки', 'escalate': False}

    agent = AsyncMock(side_effect=generate)
    monkeypatch.setattr(bot_mod, 'AsyncSessionLocal', session)
    monkeypatch.setattr(bot_mod.support_agent, 'generate_answer', agent)
    monkeypatch.setattr(bot_mod.settings_store, 'load', AsyncMock())
    monkeypatch.setattr(bot_mod.settings_store, 'get_int', lambda key: 0)
    monkeypatch.setattr(bot_mod.settings_store, 'get_bool', lambda key: False)
    monkeypatch.setattr(bot_mod.settings, 'ADMIN_IDS', '999')
    yield bot, connection, requests, agent
    bot_mod._processed_updates.clear()


def message(**changes):
    values = dict(
        message_id=1,
        date=datetime.now(UTC),
        chat={'id': 555, 'type': 'private'},
        from_user={'id': 555, 'is_bot': False, 'first_name': 'Customer'},
        business_connection_id='business-1',
        text='Не работает VPN',
    )
    values.update(changes)
    return Message(**values)


async def dispatch(bot, msg, business=True):
    update = Update(update_id=1, **{'business_message' if business else 'message': msg})
    await bot_mod.build_dispatcher().feed_update(bot, update)


@pytest.mark.asyncio
async def test_business_answer_and_typing_use_connection_and_customer_identity(service):
    bot, _, requests, agent = service
    await dispatch(bot, message())
    assert agent.call_args.args[1:3] == (555, 'Не работает VPN')
    sent = [r for r in requests if isinstance(r, (SendMessage, SendChatAction))]
    assert len(sent) == 2
    assert all(r.chat_id == 555 and r.business_connection_id == 'business-1' for r in sent)
    reads = [r for r in requests if isinstance(r, ReadBusinessMessage)]
    assert len(reads) == 1
    assert (reads[0].business_connection_id, reads[0].chat_id, reads[0].message_id) == ('business-1', 555, 1)
    assert requests.index(reads[0]) > next(i for i, r in enumerate(requests) if isinstance(r, SendMessage))


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['disabled', 'no_rights', 'no_reply', 'owner', 'bot', 'sent_by_bot', 'no_sender'])
async def test_business_ignores_ineligible_messages(service, monkeypatch, case):
    bot, connection, requests, agent = service
    changes = {}
    if case == 'disabled':
        connection = connection.model_copy(update={'is_enabled': False})
    elif case == 'no_rights':
        connection = connection.model_copy(update={'rights': None})
    elif case == 'no_reply':
        connection = connection.model_copy(update={'rights': connection.rights.model_copy(update={'can_reply': False})})
    elif case == 'owner':
        changes['from_user'] = connection.user
    elif case == 'bot':
        changes['from_user'] = {'id': 777, 'is_bot': True, 'first_name': 'Bot'}
    elif case == 'sent_by_bot':
        changes['sender_business_bot'] = {'id': 777, 'is_bot': True, 'first_name': 'Bot'}
    elif case == 'no_sender':
        changes['from_user'] = None
    monkeypatch.setattr(bot, 'get_business_connection', AsyncMock(return_value=connection))
    await dispatch(bot, message(**changes))
    agent.assert_not_called()
    assert not any(isinstance(r, SendMessage) for r in requests)


@pytest.mark.asyncio
async def test_connection_checked_again_after_permission_revoked(service, monkeypatch):
    bot, connection, requests, agent = service
    await dispatch(bot, message())
    connection = connection.model_copy(update={'rights': connection.rights.model_copy(update={'can_reply': False})})
    lookup = AsyncMock(return_value=connection)
    monkeypatch.setattr(bot, 'get_business_connection', lookup)
    await dispatch(bot, message(message_id=2))
    assert agent.call_count == 1
    lookup.assert_awaited_once_with('business-1')


@pytest.mark.asyncio
async def test_connection_lookup_failure_does_not_answer_as_regular_bot(service, monkeypatch):
    bot, _, requests, agent = service
    monkeypatch.setattr(
        bot,
        'get_business_connection',
        AsyncMock(
            side_effect=TelegramBadRequest(
                method=GetBusinessConnection(business_connection_id='business-1'),
                message='Not found',
            )
        ),
    )
    await dispatch(bot, message())
    agent.assert_not_called()
    assert requests == []


@pytest.mark.asyncio
async def test_business_escalation_admin_notification_uses_regular_bot(service):
    bot, _, requests, agent = service
    agent.side_effect = None
    agent.return_value = {'answer': 'Передаю оператору', 'escalate': True}
    await dispatch(bot, message())
    sent = [r for r in requests if isinstance(r, SendMessage)]
    assert [(r.chat_id, r.business_connection_id) for r in sent] == [(555, 'business-1'), (900, None), (999, None)]
    assert not any(isinstance(r, ReadBusinessMessage) for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['photo_disabled', 'generation_error'])
async def test_business_validation_and_error_replies_keep_connection(service, monkeypatch, case):
    bot, _, requests, agent = service
    msg = message()
    if case == 'photo_disabled':
        msg = message(text=None, photo=[{'file_id': 'photo', 'file_unique_id': 'unique', 'width': 10, 'height': 10}])
    elif case == 'generation_error':
        agent.side_effect = OpenAIError('Unavailable')
    await dispatch(bot, msg)
    sent = [r for r in requests if isinstance(r, SendMessage)]
    assert len(sent) == 1
    assert sent[0].chat_id == 555
    assert sent[0].business_connection_id == 'business-1'
    assert not any(isinstance(r, ReadBusinessMessage) for r in requests)


@pytest.mark.asyncio
async def test_duplicate_detection_separates_regular_and_business_chats(service):
    bot, _, requests, agent = service
    await dispatch(bot, message())
    await dispatch(bot, message())
    await dispatch(bot, message(business_connection_id=None), business=False)
    await dispatch(bot, message(business_connection_id='business-2'))
    assert agent.call_count == 3
    sent = [r for r in requests if isinstance(r, SendMessage)]
    assert [r.business_connection_id for r in sent] == ['business-1', None, 'business-2']


@pytest.mark.asyncio
async def test_connection_updates_registered(service):
    bot, connection, _, agent = service
    dp = bot_mod.build_dispatcher()
    assert {'message', 'business_message', 'business_connection'} <= set(dp.resolve_used_update_types())
    await dp.feed_update(bot, Update(update_id=1, business_connection=connection))
    agent.assert_not_called()


@pytest.mark.asyncio
async def test_escalation_mid_dialogue_leaves_last_message_unread(service):
    bot, _, requests, agent = service
    await dispatch(bot, message(message_id=1))
    agent.side_effect = None
    agent.return_value = {'answer': 'Передаю оператору', 'escalate': True}
    await dispatch(bot, message(message_id=2, text='Позовите оператора'))
    assert [r.message_id for r in requests if isinstance(r, ReadBusinessMessage)] == [1]
    assert agent.call_count == 2


@pytest.mark.asyncio
async def test_answer_without_read_permission_still_delivered(service, monkeypatch):
    bot, connection, requests, _ = service
    connection = connection.model_copy(
        update={'rights': connection.rights.model_copy(update={'can_read_messages': False})}
    )
    monkeypatch.setattr(bot, 'get_business_connection', AsyncMock(return_value=connection))
    await dispatch(bot, message())
    assert any(isinstance(r, SendMessage) for r in requests)
    assert not any(isinstance(r, ReadBusinessMessage) for r in requests)


@pytest.mark.asyncio
async def test_read_failure_does_not_repeat_answer_or_escalate(service, monkeypatch):
    bot, _, requests, _ = service
    read = AsyncMock(
        side_effect=TelegramBadRequest(
            method=ReadBusinessMessage(business_connection_id='business-1', chat_id=555, message_id=1),
            message='Permission revoked',
        )
    )
    monkeypatch.setattr(bot, 'read_business_message', read)
    await dispatch(bot, message())
    read.assert_awaited_once_with(business_connection_id='business-1', chat_id=555, message_id=1)
    assert [r.chat_id for r in requests if isinstance(r, SendMessage)] == [555]


@pytest.mark.asyncio
async def test_failed_answer_delivery_does_not_mark_message_read(service, monkeypatch):
    bot, _, requests, _ = service
    monkeypatch.setattr(
        bot_mod,
        '_answer_safely',
        AsyncMock(
            side_effect=TelegramBadRequest(
                method=SendMessage(chat_id=555, text='Ответ'),
                message='Chat unavailable',
            )
        ),
    )
    with pytest.raises(TelegramBadRequest):
        await dispatch(bot, message())
    assert not any(isinstance(r, ReadBusinessMessage) for r in requests)


@pytest.mark.asyncio
async def test_regular_chat_does_not_use_business_read_api(service):
    bot, _, requests, _ = service
    await dispatch(bot, message(business_connection_id=None), business=False)
    assert any(isinstance(r, SendMessage) for r in requests)
    assert not any(isinstance(r, ReadBusinessMessage) for r in requests)


async def control_callback(bot, action, control_id, user_id=900):
    query = CallbackQuery(
        id='callback',
        from_user={'id': user_id, 'is_bot': False, 'first_name': 'Operator'},
        chat_instance='test',
        data=f'aisup:{action}:{control_id}',
    ).as_(bot)
    await bot_mod.build_dispatcher().feed_update(bot, Update(update_id=10, callback_query=query))


@pytest.mark.asyncio
async def test_manual_owner_message_pauses_only_its_chat_and_bot_echo_does_not(service):
    bot, connection, requests, agent = service
    await dispatch(
        bot,
        message(
            from_user=connection.user,
            message_id=2,
            text=None,
            photo=[{'file_id': 'f', 'file_unique_id': 'u', 'width': 1, 'height': 1}],
        ),
    )
    await dispatch(bot, message(message_id=3))
    agent.assert_not_called()
    assert not any(isinstance(r, (SendMessage, ReadBusinessMessage)) for r in requests)
    other = message(chat={'id': 556, 'type': 'private'}, from_user={'id': 556, 'is_bot': False, 'first_name': 'Other'})
    await dispatch(bot, other)
    assert agent.call_count == 1
    echo = message(
        chat={'id': 556, 'type': 'private'},
        from_user=connection.user,
        sender_business_bot={'id': bot.id, 'is_bot': True, 'first_name': 'AI'},
        message_id=4,
    )
    await dispatch(bot, echo)
    await dispatch(bot, other.model_copy(update={'message_id': 5}))
    assert agent.call_count == 2


@pytest.mark.asyncio
async def test_take_and_resume_buttons_with_authorization(service):
    bot, _, _, agent = service
    control = await bot_mod.business_control.get_chat('business-1', 555, 900)
    await control_callback(bot, 'take', control.id, user_id=555)
    assert not control.manual_pause
    await control_callback(bot, 'take', control.id)
    await dispatch(bot, message())
    agent.assert_not_called()
    assert control.manual_pause
    await control_callback(bot, 'resume', control.id, user_id=999)
    await dispatch(bot, message(message_id=2))
    assert agent.call_count == 1
    assert not control.manual_pause


@pytest.mark.asyncio
@pytest.mark.parametrize('resume', [False, True])
async def test_takeover_during_generation_suppresses_pending_answer_even_after_resume(service, resume):
    bot, _, requests, agent = service

    async def generate(*args, **kwargs):
        await asyncio.sleep(0)
        control = await bot_mod.business_control.get_chat('business-1', 555, 900)
        await control_callback(bot, 'take', control.id)
        if resume:
            await control_callback(bot, 'resume', control.id)
        return {'answer': 'Устаревший ответ', 'escalate': False}

    agent.side_effect = generate
    await dispatch(bot, message())
    assert not any(isinstance(r, (SendMessage, ReadBusinessMessage)) for r in requests)


@pytest.mark.asyncio
async def test_operator_message_during_generation_suppresses_pending_error(service):
    bot, connection, requests, agent = service

    async def generate(*args, **kwargs):
        await dispatch(bot, message(from_user=connection.user, message_id=2))
        raise OpenAIError('Unavailable')

    agent.side_effect = generate
    await dispatch(bot, message())
    assert not any(isinstance(r, SendMessage) for r in requests)


@pytest.mark.asyncio
async def test_escalation_notifications_have_controls(service):
    bot, _, requests, agent = service
    agent.side_effect = None
    agent.return_value = {'answer': 'Передаю оператору', 'escalate': True}
    await dispatch(bot, message())
    notices = [r for r in requests if isinstance(r, SendMessage) and r.chat_id in {900, 999}]
    assert len(notices) == 2
    for notice in notices:
        buttons = notice.reply_markup.inline_keyboard[0]
        assert [b.text for b in buttons] == ['Взять в работу', 'Вернуть ИИ']
        assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


@pytest.mark.asyncio
async def test_manage_bot_deeplink_shows_controls_only_to_owner(service, monkeypatch):
    bot, _, requests, _ = service
    control = await bot_mod.business_control.get_chat('business-1', 555, 900)

    async def find_owned(owner_id, chat_id):
        return control if owner_id == 900 and chat_id == 555 else None

    monkeypatch.setattr(bot_mod.business_control, 'find_owned_chat', find_owned)
    for sender in (900, 555):
        msg = message(
            business_connection_id=None,
            text='/start bizChat555',
            chat={'id': sender, 'type': 'private'},
            from_user={'id': sender, 'is_bot': False, 'first_name': 'User'},
        )
        await dispatch(bot, msg, business=False)
    sent = [r for r in requests if isinstance(r, SendMessage)]
    assert sent[0].chat_id == 900
    assert sent[0].reply_markup is not None
    assert sent[1].chat_id == 555
    assert sent[1].reply_markup is None


@pytest.mark.asyncio
async def test_native_send_rejection_does_not_retry_or_mark_read(service, monkeypatch):
    bot, _, requests, _ = service
    original = bot.session.make_request

    async def request(bot, method, **kwargs):
        if isinstance(method, SendMessage):
            requests.append(method)
            raise TelegramBadRequest(method=method, message='BUSINESS_PEER_USAGE_MISSING')
        return await original(bot, method, **kwargs)

    monkeypatch.setattr(bot.session, 'make_request', request)
    with pytest.raises(TelegramBadRequest):
        await dispatch(bot, message())
    assert len([r for r in requests if isinstance(r, SendMessage)]) == 1
    assert not any(isinstance(r, ReadBusinessMessage) for r in requests)


@pytest.mark.asyncio
async def test_operator_takeover_while_collecting_messages_suppresses_batch(service, monkeypatch):
    bot, connection, requests, agent = service
    monkeypatch.setattr(bot_mod, '_message_queue', bot_mod.MessageQueue(quiet_seconds=0.03, max_wait=0.06))
    pending = asyncio.create_task(dispatch(bot, message()))
    await asyncio.sleep(0.01)
    await dispatch(bot, message(from_user=connection.user, message_id=2))
    await pending
    agent.assert_not_called()
    assert not any(isinstance(r, (SendMessage, ReadBusinessMessage)) for r in requests)
