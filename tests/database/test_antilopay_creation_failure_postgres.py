"""Rejected creation must not turn into an endless phantom invoice poll."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import AntilopayPayment, Base, User
from app.services.antilopay_service import AntilopayAPIError, antilopay_service
from app.services.payment_service import PaymentService
from app.services.payment_verification_service import _fetch_antilopay_payments
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    'code,rejected', [(3, True), (4, True), (7, True), (14, True), (22, True), (5, False), (500, False), (None, False)]
)
async def test_create_failure_classification(postgres_database, monkeypatch, code, rejected):
    monkeypatch.setattr(type(settings), 'is_antilopay_enabled', lambda _: True)
    monkeypatch.setattr(
        antilopay_service, 'create_payment', AsyncMock(side_effect=AntilopayAPIError(200, 'Test refusal', code))
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add(User(id=1, email='buyer@example.com', balance_kopeks=0))
        await db.commit()
        assert await PaymentService().create_antilopay_payment(db, user_id=1, amount_kopeks=10000) is None
        payment = await db.scalar(select(AntilopayPayment))
        assert payment.status == ('failed' if rejected else 'pending')
        pending = await _fetch_antilopay_payments(db, datetime.now(UTC) - timedelta(hours=24))
        assert bool(pending) is not rejected
        assert not payment.is_paid
        assert (await db.get(User, 1)).balance_kopeks == 0


async def test_missing_legacy_invoice_is_backed_off_without_marking_paid(postgres_database, monkeypatch):
    monkeypatch.setattr(
        antilopay_service, 'check_payment', AsyncMock(side_effect=AntilopayAPIError(200, 'Payment not found', 8))
    )
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add(User(id=1, email='buyer@example.com', balance_kopeks=0))
        await db.flush()
        db.add(
            AntilopayPayment(user_id=1, order_id='masked-order', amount_kopeks=10000, status='pending', is_paid=False)
        )
        await db.commit()
        assert await PaymentService().check_antilopay_payment_status(db, 'masked-order') is None
        pending = await _fetch_antilopay_payments(db, datetime.now(UTC) - timedelta(hours=24))
        assert pending == []
        payment = await db.scalar(select(AntilopayPayment))
        assert payment.status == 'pending'
        assert not payment.is_paid
        assert (await db.get(User, 1)).balance_kopeks == 0


@pytest.mark.parametrize('failure', [TimeoutError(), AntilopayAPIError(200, 'Rejected', 14)])
async def test_late_payment_survives_failed_create(postgres_database, monkeypatch, failure):
    monkeypatch.setattr(type(settings), 'is_antilopay_enabled', lambda _: True)
    monkeypatch.setattr(antilopay_service, 'create_payment', AsyncMock(side_effect=failure))
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
    monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add(User(id=1, email='buyer@example.com', balance_kopeks=0))
        await db.commit()
        service = PaymentService()
        assert await service.create_antilopay_payment(db, user_id=1, amount_kopeks=10000) is None
        payment = await db.scalar(select(AntilopayPayment))
        if isinstance(failure, TimeoutError):
            assert payment.status == 'pending'
        payload = dict(
            type='payment',
            order_id=payment.order_id,
            payment_id='masked-payment',
            status='SUCCESS',
            original_amount=100,
            amount=95,
            currency='RUB',
        )
        assert await service.process_antilopay_callback(db, payload)
        assert await service.process_antilopay_callback(db, payload)
        assert (await db.get(User, 1)).balance_kopeks == 10000
        assert (await db.get(AntilopayPayment, payment.id)).is_paid


async def test_create_refusal_cannot_erase_winning_paid_callback(postgres_database, monkeypatch):
    monkeypatch.setattr(type(settings), 'is_antilopay_enabled', lambda _: True)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
    monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', AsyncMock())
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, other):
        db.add(User(id=1, email='buyer@example.com', balance_kopeks=0))
        await db.commit()
        service = PaymentService()

        async def create(**kwargs):
            assert await service.process_antilopay_callback(
                other,
                dict(
                    type='payment',
                    order_id=kwargs['order_id'],
                    payment_id='masked-payment',
                    status='SUCCESS',
                    original_amount=100,
                    amount=95,
                    currency='RUB',
                ),
            )
            raise AntilopayAPIError(200, 'Rejected', 14)

        monkeypatch.setattr(antilopay_service, 'create_payment', create)
        assert await service.create_antilopay_payment(db, user_id=1, amount_kopeks=10000) is None
        payment = await db.scalar(select(AntilopayPayment))
        assert payment.status == 'success'
        assert payment.is_paid
        assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10000


@pytest.mark.parametrize('known_remote', [False, True])
async def test_not_found_retry_recovers_and_keeps_real_incidents_visible(postgres_database, monkeypatch, known_remote):
    from unittest.mock import MagicMock

    from app.services.payment import antilopay as module

    logger = MagicMock()
    monkeypatch.setattr(module, 'logger', logger)
    check = AsyncMock(side_effect=AntilopayAPIError(200, 'Payment not found', 8))
    monkeypatch.setattr(antilopay_service, 'check_payment', check)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, _):
        db.add(User(id=1, email='buyer@example.com', balance_kopeks=0))
        await db.flush()
        payment = AntilopayPayment(
            user_id=1,
            order_id='masked-order',
            amount_kopeks=10000,
            status='pending',
            is_paid=False,
            antilopay_payment_id='masked-payment' if known_remote else None,
        )
        db.add(payment)
        await db.commit()
        service = PaymentService()
        for minutes in [10, 20, 40, 60, 60]:
            before = datetime.now(UTC)
            await service.check_antilopay_payment_status(db, payment.order_id)
            retry_at = datetime.fromisoformat(payment.metadata_json['antilopay_check_after'])
            assert before + timedelta(minutes=minutes) <= retry_at <= datetime.now(UTC) + timedelta(minutes=minutes)
        assert logger.error.called is known_remote
        assert logger.warning.called is not known_remote
        payment.metadata_json = {
            **payment.metadata_json,
            'antilopay_check_after': (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        await db.commit()
        assert len(await _fetch_antilopay_payments(db, datetime.now(UTC) - timedelta(hours=24))) == 1
        check.side_effect = None
        check.return_value = dict(order_id=payment.order_id, payment_id='masked-payment', status='PENDING')
        assert await service.check_antilopay_payment_status(db, payment.order_id) is not None
        assert payment.antilopay_payment_id == 'masked-payment'
        assert 'antilopay_check_after' not in payment.metadata_json
        assert 'antilopay_check_attempts' not in payment.metadata_json
