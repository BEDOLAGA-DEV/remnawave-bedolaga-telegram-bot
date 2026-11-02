"""TG-117: failed notification flush cannot hide its cause or repeat CryptoBot credit."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import CryptoBotPayment, SubscriptionEvent, Transaction, User
from app.services import admin_notification_service as notifications
from app.services.payment import cryptobot
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
TABLES = [User.__table__]


async def _seed(db, *, paid=False):
    user = User(
        telegram_id=1,
        username='masked-1',
        first_name='masked-1',
        language='ru',
        balance_kopeks=35000 if paid else 0,
        has_made_first_topup=True,
    )
    db.add(user)
    await db.flush()
    transaction = None
    if paid:
        transaction = Transaction(
            user_id=user.id,
            type='deposit',
            amount_kopeks=35000,
            payment_method='cryptobot',
            is_completed=True,
        )
        db.add(transaction)
        await db.flush()
    db.add(
        CryptoBotPayment(
            user_id=user.id,
            invoice_id='masked-invoice',
            amount='1.00',
            asset='USDT',
            status='paid' if paid else 'active',
            transaction_id=transaction.id if transaction else None,
        )
    )
    await db.commit()
    return user, transaction


def _fail_event_flush(monkeypatch):
    create_event = notifications.create_subscription_event

    async def invalid_event(db, **kwargs):
        # Настоящий PostgreSQL NOT NULL error оставляет SQLAlchemy session
        # failed и expires ORM-объекты, чего AsyncMock(exception) не воспроизводит.
        return await create_event(db, **{**kwargs, 'event_type': None})

    monkeypatch.setattr(notifications, 'create_subscription_event', invalid_event)
    errors = []

    def capture_error(message, **kwargs):
        errors.append((message, type(sys.exception()).__name__, kwargs))

    logger = MagicMock()
    logger.error.side_effect = capture_error
    monkeypatch.setattr(notifications, 'logger', logger)
    return errors


async def test_event_failure_logs_original_error_and_rolls_back(postgres_database, monkeypatch):
    async with postgres_session(postgres_database, TABLES) as db:
        user, transaction = await _seed(db, paid=True)
        errors = _fail_event_flush(monkeypatch)
        service = notifications.AdminNotificationService(MagicMock())

        recorded = await service._record_subscription_event(
            db,
            event_type='balance_topup',
            user=user,
            subscription=None,
            transaction=transaction,
            amount_kopeks=35000,
        )

        assert recorded is False
        assert db.is_active
        assert [kind for _, kind, _ in errors] == ['IntegrityError']
        assert errors[0][2]['exc_info'] is True
        assert await db.scalar(select(User.balance_kopeks)) == 35000
        assert await db.scalar(select(func.count(Transaction.id))) == 1
        assert await db.scalar(select(func.count(SubscriptionEvent.id))) == 0


@pytest.mark.parametrize('event_flush_fails', [False, True])
async def test_admin_event_does_not_repeat_cryptobot_credit(postgres_database, monkeypatch, event_flush_fails):
    from app.database.crud import transaction as transaction_crud
    from app.services import referral_service
    from app.services.payment import common

    async with postgres_session(postgres_database, TABLES) as db:
        await _seed(db)
        errors = _fail_event_flush(monkeypatch) if event_flush_fails else []
        monkeypatch.setattr(cryptobot, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        monkeypatch.setattr(cryptobot.currency_converter, 'usd_to_rub', AsyncMock(return_value=350.0))
        monkeypatch.setattr(transaction_crud, 'emit_transaction_side_effects', AsyncMock())
        monkeypatch.setattr(referral_service, 'process_referral_topup', AsyncMock())
        monkeypatch.setattr(common, 'send_cart_notification_after_topup', AsyncMock())
        monkeypatch.setattr(notifications.AdminNotificationService, '_is_enabled', lambda self: True)
        admin_send = AsyncMock(return_value=True)
        monkeypatch.setattr(notifications.AdminNotificationService, '_send_message', admin_send)
        service = cryptobot.CryptoBotPaymentMixin()
        service.bot = MagicMock(send_message=AsyncMock())
        service.build_topup_success_keyboard = AsyncMock(return_value=None)
        payload = {'update_type': 'invoice_paid', 'payload': {'invoice_id': 'masked-invoice'}}

        assert await service.process_cryptobot_webhook(db, payload) is True
        assert admin_send.await_count == 1
        assert '350' in admin_send.call_args.args[0]
        assert [kind for _, kind, _ in errors] == (['IntegrityError'] if event_flush_fails else [])
        assert await db.scalar(select(User.balance_kopeks)) == 35000
        assert await db.scalar(select(func.count(Transaction.id))) == 1
        payment = await db.scalar(select(CryptoBotPayment))
        assert payment.status == 'paid'
        assert payment.transaction_id is not None

        assert await service.process_cryptobot_webhook(db, payload) is True
        assert await db.scalar(select(User.balance_kopeks)) == 35000
        assert await db.scalar(select(func.count(Transaction.id))) == 1
        assert admin_send.await_count == 1
        assert await db.scalar(select(func.count(SubscriptionEvent.id))) == int(not event_flush_fails)


@pytest.mark.parametrize('loader', ['get_user_by_id', 'get_transaction_by_id'])
async def test_topup_reload_failure_does_not_read_expired_orm_in_error_log(postgres_database, monkeypatch, loader):
    async with postgres_session(postgres_database, TABLES) as db:
        user, transaction = await _seed(db, paid=True)
        errors = _fail_event_flush(monkeypatch)
        monkeypatch.setattr(notifications, loader, AsyncMock(side_effect=RuntimeError('masked-load-error')))
        service = notifications.AdminNotificationService(MagicMock())
        service._send_message = AsyncMock(return_value=True)
        monkeypatch.setattr(service, '_is_enabled', lambda: True)

        result = await service.send_balance_topup_notification(
            user,
            transaction,
            0,
            topup_status='masked',
            referrer_info='Нет',
            subscription=None,
            promo_group=None,
            db=db,
        )

        assert result is False
        assert [kind for _, kind, _ in errors] == ['IntegrityError', 'RuntimeError']
        assert db.is_active
        service._send_message.assert_not_awaited()


async def test_rollback_failure_also_logs_scalar_identity(postgres_database, monkeypatch):
    async with postgres_session(postgres_database, TABLES) as db:
        user, transaction = await _seed(db, paid=True)
        errors = _fail_event_flush(monkeypatch)
        rollback = db.rollback
        monkeypatch.setattr(db, 'rollback', AsyncMock(side_effect=RuntimeError('masked-rollback-error')))
        service = notifications.AdminNotificationService(MagicMock())

        recorded = await service._record_subscription_event(
            db,
            event_type='balance_topup',
            user=user,
            subscription=None,
            transaction=transaction,
            amount_kopeks=35000,
        )

        assert recorded is False
        assert [kind for _, kind, _ in errors] == ['IntegrityError', 'RuntimeError']
        assert all(details['user_id'] == 1 for _, _, details in errors)
        await rollback()
