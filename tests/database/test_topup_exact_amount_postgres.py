"""Manual retry at the provider minimum must persist exactly the typed kopecks."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from app.config import settings
from app.database.models import Base, PlategaPayment, Transaction, User
from app.handlers.balance import main, platega
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from tests.fixtures.postgres_db import postgres_session


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_rejected_prefill_then_exact_retry_creates_exact_pending_invoice(postgres_database, monkeypatch):
    monkeypatch.setattr(type(settings), 'is_platega_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 6407)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 10000)
    monkeypatch.setattr(platega, '_get_active_methods', lambda: [2])
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    service.platega_service = SimpleNamespace(
        is_configured=True,
        create_payment=AsyncMock(
            return_value={
                'transactionId': 'local-exact-invoice',
                'redirect': 'https://example.com/pay',
                'status': 'PENDING',
            }
        ),
    )
    monkeypatch.setattr(platega, 'PaymentService', lambda *_args: service)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=1001, username='localbuyer', language='ru', status='active', balance_kopeks=6000)
        db.add(user)
        await db.commit()

        @asynccontextmanager
        async def session():
            yield db

        monkeypatch.setattr('app.database.database.AsyncSessionLocal', session)
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1001, user_id=1001))
        await state.set_state(BalanceStates.waiting_for_amount)
        selection = {'payment_method': 'platega', 'platega_method': 2, 'saved_cart': {'total_price': 10000}}
        await state.set_data(selection)
        message = SimpleNamespace(
            text='40',
            chat=SimpleNamespace(id=1001),
            message_id=10,
            bot=SimpleNamespace(),
            answer=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=1001), message_id=11)),
            delete=AsyncMock(),
        )
        await main.process_topup_amount(message, user, state)
        assert await db.scalar(select(func.count()).select_from(PlategaPayment)) == 0
        service.platega_service.create_payment.assert_not_awaited()
        assert await state.get_state() == BalanceStates.waiting_for_amount.state
        assert await state.get_data() == selection

        message.text = '64.07'
        await main.process_topup_amount(message, user, state)

        invoice = (await db.execute(select(PlategaPayment))).scalar_one()
        assert invoice.amount_kopeks == 6407
        assert invoice.is_paid is False
        assert invoice.user_id == 1
        service.platega_service.create_payment.assert_awaited_once()
        assert service.platega_service.create_payment.await_args.kwargs['amount'] == 64.07
        await db.refresh(user)
        assert user.balance_kopeks == 6000
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 0
