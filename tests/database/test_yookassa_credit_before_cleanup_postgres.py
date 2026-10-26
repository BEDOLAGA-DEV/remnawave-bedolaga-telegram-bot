"""Confirmed top-ups must not wait for Telegram invoice-message cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.yookassa import create_yookassa_payment
from app.database.models import Base, SavedPaymentMethod, Transaction, User
from app.services.payment_service import PaymentService
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('cleanup', ['success', 'failure', 'cancel', 'hang'])
async def test_yookassa_credit_is_committed_while_invoice_cleanup_waits(postgres_database, monkeypatch, cleanup):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', True)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
    monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', AsyncMock())
    monkeypatch.setattr(
        'app.services.admin_notification_service.AdminNotificationService.send_balance_topup_notification', AsyncMock()
    )
    deleting, release = asyncio.Event(), asyncio.Event()

    async def slow_delete(chat_id, message_id):
        deleting.set()
        if cleanup == 'hang':
            await asyncio.Future()
        await asyncio.wait_for(release.wait(), 10)
        if cleanup == 'failure':
            raise TimeoutError('Telegram timeout')
        if cleanup == 'cancel':
            raise asyncio.CancelledError

    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables), count=2) as (db, observer):
        user = User(status='active', balance_kopeks=0, language='ru', has_made_first_topup=True)
        db.add(user)
        await db.commit()
        user_id = user.id
        await create_yookassa_payment(
            db,
            user_id=user_id,
            yookassa_payment_id='masked-payment',
            amount_kopeks=10000,
            currency='RUB',
            description='Test topup',
            status='pending',
            metadata_json={'invoice_message': {'chat_id': 1, 'message_id': 2}},
        )
        service = PaymentService.__new__(PaymentService)
        service.bot = SimpleNamespace(delete_message=slow_delete)
        service.nalogo_service = None
        service.yookassa_service = SimpleNamespace(
            get_payment_info=AsyncMock(
                return_value={
                    'id': 'masked-payment',
                    'status': 'succeeded',
                    'paid': True,
                    'amount_value': 100,
                    'amount_currency': 'RUB',
                    'payment_method_id': 'masked-method',
                    'payment_method_saved': True,
                    'payment_method_type': 'bank_card',
                }
            )
        )
        event = {
            'event': 'payment.succeeded',
            'object': {
                'id': 'masked-payment',
                'status': 'succeeded',
                'paid': True,
                'payment_method': {'id': 'masked-method', 'saved': True, 'type': 'bank_card'},
            },
        }
        task = asyncio.create_task(service.process_yookassa_webhook(db, event))
        try:
            await asyncio.wait_for(deleting.wait(), 5)
            saved_user = await observer.get(User, user_id)
            assert saved_user.balance_kopeks == 10000
            ledger = list(await observer.scalars(select(Transaction)))
            assert len(ledger) == 1 and ledger[0].amount_kopeks == 10000
            methods = list(await observer.scalars(select(SavedPaymentMethod)))
            assert len(methods) == 1 and methods[0].yookassa_payment_method_id == 'masked-method'
        finally:
            release.set()
            results = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 7)
        if cleanup == 'cancel':
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0] is True
        assert await service.process_yookassa_webhook(db, event) is True
        await observer.refresh(saved_user)
        assert saved_user.balance_kopeks == 10000
        assert len(list(await observer.scalars(select(Transaction)))) == 1
        assert len(list(await observer.scalars(select(SavedPaymentMethod)))) == 1
