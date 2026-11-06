from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, Ticket, TicketMessage, User
from app.handlers import tickets
from app.services.event_emitter import event_emitter
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def test_payment_support_creates_owned_ticket_and_preserves_balance(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', 'tickets')
    monkeypatch.setattr(tickets, 'cache', SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock()))
    monkeypatch.setattr(tickets.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(tickets, '_try_delete_message_later', AsyncMock())
    monkeypatch.setattr(tickets, 'notify_admins_about_new_ticket', AsyncMock())
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=1) as sessions:
        db = sessions[0]
        user = User(id=1, telegram_id=42, language='en', balance_kopeks=12345)
        db.add(user)
        await db.commit()
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=123456, chat_id=42, user_id=42))
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        message = SimpleNamespace(
            chat=SimpleNamespace(id=42),
            message_id=10,
            edit_text=AsyncMock(),
            answer=AsyncMock(),
            bot=bot,
            text='My Stars payment has not arrived.',
            caption=None,
            photo=None,
            video=None,
        )
        callback = SimpleNamespace(message=message, answer=AsyncMock())
        await tickets.start_payment_ticket(callback, state, user, db)
        await tickets.handle_ticket_message_input(message, state, user, db)
        created = (await db.scalars(select(Ticket))).one()
        body = (await db.scalars(select(TicketMessage))).one()
        assert created.user_id == user.id
        assert created.title == 'Payment issue'
        assert body.message_text == message.text
        assert body.user_id == user.id
        assert user.balance_kopeks == 12345
        assert await state.get_state() is None
        tickets.notify_admins_about_new_ticket.assert_awaited_once()
        await storage.close()
