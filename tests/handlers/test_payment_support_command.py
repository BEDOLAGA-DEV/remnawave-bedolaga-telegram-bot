from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from app.config import settings
from app.handlers import support
from app.services.bot_commands import build_menu


def test_paysupport_menu_tracks_stars(monkeypatch):
    for enabled in (True, False, True):
        monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', enabled)
        for admin in (True, False):
            assert ('paysupport' in [c.command for c in build_menu('en', admin=admin)]) is enabled


@pytest.mark.parametrize('mode', ['tickets', 'both', 'contact'])
async def test_payment_support_uses_configured_channels(monkeypatch, mode):
    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', mode)
    monkeypatch.setattr(settings, 'SUPPORT_MENU_ENABLED', False)
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@masked_support')
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(language_code='en')
    message.answer = AsyncMock()
    user = SimpleNamespace(language='en', status='active')
    await support.payment_support_command(message, db_user=user)
    keys = message.answer.call_args.kwargs['reply_markup'].inline_keyboard
    assert any(b.url == 'https://t.me/masked_support' for row in keys for b in row)
    assert any(b.callback_data == 'payment_support_create' for row in keys for b in row) is (mode != 'contact')


@pytest.mark.parametrize(
    'user', [None, SimpleNamespace(language='en', status='blocked'), SimpleNamespace(language='en', status='deleted')]
)
async def test_payment_support_contact_for_inaccessible_accounts(monkeypatch, user):
    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'tickets')
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@masked_support')
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(language_code='en')
    message.answer = AsyncMock()
    await support.payment_support_command(message, db_user=user)
    keys = message.answer.call_args.kwargs['reply_markup'].inline_keyboard
    assert any(b.url for row in keys for b in row)
    assert not any(b.callback_data == 'payment_support_create' for row in keys for b in row)


async def test_command_routes_inside_fsm_without_clearing_it(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@masked_support')
    bot = Bot('123456:masked-test-token')
    bot.get_me = AsyncMock(return_value=User(id=123456, is_bot=True, first_name='Bot', username='masked_bot'))
    bot.session = AsyncMock()
    dp = Dispatcher(storage=MemoryStorage())
    support.register_command_handlers(dp)
    swallowed = AsyncMock()

    async def fallback(message):
        await swallowed(message)

    dp.message.register(fallback)
    state = dp.fsm.get_context(bot=bot, chat_id=42, user_id=42)
    await state.set_state('Purchase:waiting_amount')
    await state.set_data({'cart': 'masked'})
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type='private'),
        from_user=User(id=42, is_bot=False, first_name='User'),
        text='/paysupport@masked_bot',
    )
    await dp.feed_update(bot, Update(update_id=1, message=message), db_user=None)
    swallowed.assert_not_awaited()
    assert await state.get_state() == 'Purchase:waiting_amount'
    assert await state.get_data() == {'cart': 'masked'}
    assert bot.session.await_count == 1
    await dp.storage.close()


@pytest.mark.parametrize('status', [None, 'blocked', 'deleted', 'active'])
async def test_real_middleware_chain_keeps_read_only_payment_help_available(monkeypatch, status):
    from contextlib import asynccontextmanager

    from app.middlewares import auth
    from app.middlewares.blacklist import BlacklistMiddleware, blacklist_service
    from app.middlewares.channel_checker import ChannelCheckerMiddleware
    from app.middlewares.display_name_restriction import DisplayNameRestrictionMiddleware
    from app.middlewares.maintenance import MaintenanceMiddleware, maintenance_service
    from app.middlewares.subscription_checker import SubscriptionStatusMiddleware

    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@masked_support')
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', False)
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', True)
    monkeypatch.setattr(maintenance_service, 'is_maintenance_active', lambda: True)
    blacklist = AsyncMock(return_value=(True, 'masked'))
    monkeypatch.setattr(blacklist_service, 'is_user_blacklisted', blacklist)
    db = AsyncMock()
    user = None if status is None else SimpleNamespace(id=1, language='en', status=status)
    monkeypatch.setattr(auth, 'get_user_by_telegram_id', AsyncMock(return_value=user))

    @asynccontextmanager
    async def sessions():
        yield db

    monkeypatch.setattr(auth, 'AsyncSessionLocal', sessions)
    bot = Bot('123456:masked-test-token')
    bot.session = AsyncMock()
    dp = Dispatcher(storage=MemoryStorage())
    for middleware in (
        MaintenanceMiddleware(),
        BlacklistMiddleware(),
        ChannelCheckerMiddleware(),
        auth.AuthMiddleware(),
        DisplayNameRestrictionMiddleware(),
        SubscriptionStatusMiddleware(),
    ):
        dp.message.middleware(middleware)
    support.register_command_handlers(dp)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type='private'),
        from_user=User(id=42, is_bot=False, first_name='https://masked.example', language_code='en'),
        text='/paysupport',
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))
    assert bot.session.await_count == 1
    assert 'Payment support' in bot.session.call_args.args[1].text
    db.commit.assert_not_awaited()
    blacklist.assert_not_awaited()
    await dp.storage.close()


async def test_payment_ticket_prefills_title_and_preserves_ticket_checks(monkeypatch):
    from app.handlers import tickets

    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'tickets')
    monkeypatch.setattr(tickets.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    monkeypatch.setattr(tickets.TicketCRUD, 'user_has_active_ticket', AsyncMock(return_value=False))
    storage = MemoryStorage()
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    state = FSMContext(storage, StorageKey(bot_id=123456, chat_id=42, user_id=42))
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock(), chat=SimpleNamespace(id=42), message_id=10), answer=AsyncMock()
    )
    user = SimpleNamespace(id=1, language='en')
    await tickets.start_payment_ticket(callback, state, user, AsyncMock())
    assert await state.get_state() == tickets.TicketStates.waiting_for_message.state
    assert (await state.get_data())['title'] == 'Payment issue'
    await state.clear()
    monkeypatch.setattr(tickets.TicketCRUD, 'user_has_active_ticket', AsyncMock(return_value=True))
    await tickets.start_payment_ticket(callback, state, user, AsyncMock())
    assert await state.get_state() is None
    assert callback.answer.call_args.kwargs['show_alert'] is True
    await storage.close()


def test_support_bypass_requires_server_flag_and_private_chat():
    from app.utils.support_access import is_support_handler

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type='private'),
        from_user=User(id=42, is_bot=False, first_name='User'),
        text='/paysupport',
    )
    assert not is_support_handler(message, {})
    flags = {'handler': SimpleNamespace(flags={'public_payment_support': True})}
    assert is_support_handler(message, flags)
    assert not is_support_handler(message.model_copy(update={'chat': Chat(id=-42, type='group')}), flags)
    ticket_flags = {'handler': SimpleNamespace(flags={'support_access': True})}
    assert is_support_handler(message, ticket_flags)
    assert not is_support_handler(message, ticket_flags, payment_only=True)


async def test_return_to_support_keeps_non_security_gates_open(monkeypatch):
    from aiogram.types import CallbackQuery

    from app.middlewares.channel_checker import ChannelCheckerMiddleware
    from app.middlewares.display_name_restriction import DisplayNameRestrictionMiddleware
    from app.middlewares.maintenance import MaintenanceMiddleware, maintenance_service

    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', True)
    monkeypatch.setattr(maintenance_service, 'is_maintenance_active', lambda: True)
    render = AsyncMock()
    monkeypatch.setattr(support, 'edit_or_answer_photo', render)
    bot = Bot('123456:masked-test-token')
    bot.session = AsyncMock()
    dp = Dispatcher(storage=MemoryStorage())
    for middleware in (MaintenanceMiddleware(), ChannelCheckerMiddleware(), DisplayNameRestrictionMiddleware()):
        dp.callback_query.middleware(middleware)
    support.register_handlers(dp)
    user = User(id=42, is_bot=False, first_name='https://masked.example')
    message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type='private'), text='Return')
    callback = CallbackQuery(
        id='masked-query', from_user=user, chat_instance='masked', message=message, data='menu_support'
    )
    await dp.feed_update(bot, Update(update_id=1, callback_query=callback), db_user=SimpleNamespace(language='en'))
    render.assert_awaited_once()
    await dp.storage.close()


async def test_switching_to_contact_cancels_pending_payment_ticket(monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.handlers import tickets

    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'contact')
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@masked_support')
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=123456, chat_id=42, user_id=42))
    await state.set_state(tickets.TicketStates.waiting_for_message)
    await state.set_data({'payment_support': True, 'title': 'Payment issue'})
    create = AsyncMock()
    monkeypatch.setattr(tickets.TicketCRUD, 'create_ticket', create)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    await tickets.handle_ticket_message_input(
        message, state, SimpleNamespace(language='en', status='active'), AsyncMock()
    )
    create.assert_not_awaited()
    assert await state.get_state() is None
    assert message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url == 'https://t.me/masked_support'
    await storage.close()
