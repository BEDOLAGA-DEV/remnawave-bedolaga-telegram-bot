"""Real recurring callback -> PostgreSQL -> both admin payment surfaces (#3279)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.cabinet.routes.admin_payments import _record_to_response
from app.database.crud import platega_subscription as sub_crud
from app.database.models import Base, PaymentMethod, PlategaPayment, Subscription, Transaction, TransactionType, User
from app.services.payment.platega import PlategaPaymentMixin
from app.services.payment_search_service import SearchParams, StatusFilter, search_payments, search_payments_stats
from app.services.payment_verification_service import get_payment_record, list_recent_pending_payments
from tests.fixtures.postgres_db import postgres_session


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


async def test_real_recurring_callback_is_visible_once_after_replay(postgres_database, monkeypatch):
    # Only external effects are replaced; the callback, transaction and read queries are real.
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.update_remnawave_user', AsyncMock())
    monkeypatch.setattr(PlategaPaymentMixin, '_notify_sbp_recurring', AsyncMock())

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        user = User(id=1, telegram_id=1001, username='localbuyer', language='ru', status='active', balance_kopeks=50000)
        db.add(user)
        await db.flush()
        end = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end)
        db.add(subscription)
        await db.commit()
        record = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='local-subscription',
            status='ACTIVE',
        )
        callback = {
            'Status': 'CONFIRMED',
            'Id': 'local-charge',
            'Amount': 199,
            'Currency': 'RUB',
            'PaymentMethod': 6,
            'SubscriptionId': 'local-subscription',
        }
        service = PlategaPaymentMixin()
        await service.process_platega_subscription_callback(db, callback)
        await service.process_platega_subscription_callback(db, callback)
        await db.refresh(record)
        await db.refresh(subscription)
        await db.refresh(user)
        assert record.charges_success == 1
        assert subscription.end_date == end + timedelta(days=30)
        assert user.balance_kopeks == 50000
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 1
        assert await db.scalar(select(Transaction.amount_kopeks)) == -19900
        assert await db.scalar(select(func.count()).select_from(PlategaPayment)) == 0

        records = await list_recent_pending_payments(db)
        assert len(records) == 1
        payment = records[0]
        assert payment.method == PaymentMethod.PLATEGA_RECURRENT
        assert payment.identifier == 'local-charge'
        assert payment.amount_kopeks == 19900
        response = _record_to_response(payment)
        assert response.is_paid
        assert not response.is_checkable
        assert response.user_telegram_id == 1001
        assert 'СБП' in response.method_display
        details = await get_payment_record(db, payment.method, payment.local_id)
        assert details is not None and details.identifier == 'local-charge'
        assert details.amount_kopeks == 19900

        for query in [None, 'local-charge', '@localbuyer', '1001']:
            params = SearchParams(
                search=query, status_filter=StatusFilter.PAID, method_filter=PaymentMethod.PLATEGA_RECURRENT
            )
            found, total = await search_payments(db, params)
            assert total == 1
            assert found[0].identifier == 'local-charge'
            assert found[0].amount_kopeks == 19900
            stats = await search_payments_stats(db, params)
            assert stats.total == stats.paid == 1
            assert stats.pending == 0
            assert stats.by_method == {'platega_recurrent': 1}


async def test_recurring_read_queries_exclude_wallet_deposits_and_old_charges(postgres_database):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=1001, language='ru', status='active', balance_kopeks=50000))
        await db.flush()
        now = datetime.now(UTC)
        for local_id, kind, method, created in [
            (1, TransactionType.SUBSCRIPTION_PAYMENT, PaymentMethod.PLATEGA, now),
            (2, TransactionType.SUBSCRIPTION_PAYMENT, PaymentMethod.BALANCE, now),
            (3, TransactionType.DEPOSIT, PaymentMethod.PLATEGA, now),
            (4, TransactionType.SUBSCRIPTION_PAYMENT, PaymentMethod.PLATEGA, now - timedelta(days=2)),
        ]:
            db.add(
                Transaction(
                    id=local_id,
                    user_id=1,
                    type=kind.value,
                    payment_method=method.value,
                    amount_kopeks=-19900,
                    is_completed=True,
                    external_id=f'local-{local_id}',
                    created_at=created,
                )
            )
        # Same numeric id in a different table must still open the provider invoice.
        db.add(
            PlategaPayment(
                id=1,
                user_id=1,
                amount_kopeks=50000,
                status='PENDING',
                is_paid=False,
                created_at=now,
                platega_transaction_id='local-invoice',
                correlation_id='local-correlation',
                payment_method_code=2,
            )
        )
        await db.commit()

        records = await list_recent_pending_payments(db)
        assert {(p.method, p.identifier) for p in records} == {
            (PaymentMethod.PLATEGA_RECURRENT, 'local-1'),
            (PaymentMethod.PLATEGA, 'local-invoice'),
        }
        found, total = await search_payments(db, SearchParams(status_filter=StatusFilter.PAID))
        assert total == 1
        assert found[0].identifier == 'local-1'
        assert found[0].amount_kopeks == 19900
        for local_id in [2, 3]:
            assert await get_payment_record(db, PaymentMethod.PLATEGA_RECURRENT, local_id) is None
        provider = await get_payment_record(db, PaymentMethod.PLATEGA, 1)
        assert provider is not None and provider.identifier == 'local-invoice'
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 4
