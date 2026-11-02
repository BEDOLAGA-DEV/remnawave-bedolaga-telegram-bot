"""Paid landing purchases have a stable admin identity independent of invoice/user creation."""

import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import admin_payments
from app.cabinet.routes.admin_payments import _record_to_response
from app.database.models import Base, CryptoBotPayment, GuestPurchase, PaymentMethod, PlategaPayment, Transaction, User
from app.handlers.admin.payments import _build_detail_keyboard, _build_list_keyboard, _build_payment_details_text
from app.localization.texts import get_texts
from app.services.payment.common import try_fulfill_guest_purchase
from app.services.payment_search_service import SearchParams, StatusFilter, search_payments, search_payments_stats
from app.services.payment_verification_service import get_payment_record, list_recent_pending_payments
from tests.fixtures.postgres_db import postgres_session


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize('method', ['yookassa_sbp', 'platega_2', 'cryptobot', 'pal24', 'wata'])
@pytest.mark.parametrize('gift', [False, True])
async def test_paid_landing_purchase_visible_without_provider_user(postgres_database, method, gift):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        now = datetime.now(UTC)
        db.add(
            GuestPurchase(
                id=1,
                token='local-purchase',
                contact_type='email',
                contact_value='local@example.com',
                period_days=30,
                amount_kopeks=19900,
                payment_method=method,
                payment_id='local-invoice',
                source='landing',
                is_gift=gift,
                status='paid' if gift else 'delivered',
                paid_at=now,
                created_at=now - timedelta(days=2),
            )
        )
        await db.commit()
        records = await list_recent_pending_payments(db)
        assert len(records) == 1
        payment = records[0]
        assert payment.source == 'landing'
        assert payment.identifier == 'local-invoice'
        assert payment.amount_kopeks == 19900
        assert payment.user is None
        assert payment.is_paid
        response = _record_to_response(payment)
        assert response.source == 'landing'
        assert not response.is_checkable
        assert response.user_id is None
        assert 'лендинг' in response.method_display.lower()
        keyboard = _build_list_keyboard(records, page=1, total_pages=1, language='ru')
        assert keyboard.inline_keyboard[0][0].callback_data == f'admin_payment_landing_{payment.method.value}_1'
        _build_detail_keyboard(payment, language='ru')
        text = _build_payment_details_text(payment, texts=get_texts('ru'), language='ru')
        assert 'local-invoice' in text
        assert 'local-purchase' not in text  # Purchase tokens are claim credentials, not payment identifiers.
        details = await get_payment_record(db, payment.method, payment.local_id, source='landing')
        assert details is not None and details.identifier == 'local-invoice'
        params = SearchParams(search='local-invoice', status_filter=StatusFilter.PAID, method_filter=payment.method)
        found, total = await search_payments(db, params)
        assert total == 1 and found[0].source == 'landing'
        stats = await search_payments_stats(db, params)
        assert stats.total == stats.paid == 1
        assert stats.by_method == {payment.method.value: 1}
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 0


async def test_landing_deduplicates_provider_and_transaction_but_preserves_other_payment(postgres_database):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        now = datetime.now(UTC)
        db.add(
            User(id=1, telegram_id=1001, username='localbuyer', language='ru', status='active', balance_kopeks=50000)
        )
        await db.flush()
        db.add(
            GuestPurchase(
                id=1,
                token='local-purchase',
                contact_type='email',
                contact_value='local@example.com',
                period_days=30,
                amount_kopeks=19900,
                payment_method='platega_2',
                payment_id='local-invoice',
                source='landing',
                status='delivered',
                paid_at=now,
                user_id=1,
            )
        )
        db.add(
            PlategaPayment(
                id=1,
                user_id=1,
                amount_kopeks=19900,
                status='PENDING',
                is_paid=False,
                platega_transaction_id='local-provider-id',
                correlation_id='local-invoice',
                payment_method_code=2,
                metadata_json={'purpose': 'guest_purchase', 'purchase_token': 'local-purchase'},
            )
        )
        db.add_all(
            [
                Transaction(
                    id=1,
                    user_id=1,
                    type='subscription_payment',
                    payment_method='platega',
                    external_id='local-invoice',
                    amount_kopeks=-19900,
                    is_completed=True,
                    created_at=now,
                ),
                Transaction(
                    id=2,
                    user_id=1,
                    type='subscription_payment',
                    payment_method='balance',
                    amount_kopeks=-19900,
                    is_completed=True,
                    created_at=now,
                ),
                Transaction(
                    id=3,
                    user_id=1,
                    type='subscription_payment',
                    payment_method='platega',
                    external_id='real-recurring',
                    amount_kopeks=-19900,
                    is_completed=True,
                    created_at=now,
                ),
            ]
        )
        await db.commit()
        records = await list_recent_pending_payments(db)
        assert {(p.source, p.identifier) for p in records} == {
            ('landing', 'local-invoice'),
            ('provider', 'real-recurring'),
        }
        for query in ['local-invoice', '@localbuyer', '1001', 'local@example.com']:
            found, total = await search_payments(db, SearchParams(search=query, method_filter=PaymentMethod.PLATEGA))
            assert total == 1 and found[0].source == 'landing'
        found, total = await search_payments(db, SearchParams(search='local-invoice'))
        assert total == 1 and found[0].source == 'landing'
        assert await get_payment_record(db, PaymentMethod.YOOKASSA, 1, source='landing') is None
        provider = await get_payment_record(db, PaymentMethod.PLATEGA, 1)
        assert provider is not None and provider.identifier == 'local-provider-id'
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 3


@pytest.mark.parametrize(
    'method,status,paid,source',
    [
        ('balance', 'delivered', True, 'bot'),
        ('manual', 'delivered', True, 'landing'),
        ('yookassa', 'pending', False, 'landing'),
        ('yookassa', 'failed', False, 'landing'),
    ],
)
async def test_internal_and_unpaid_purchases_are_excluded(postgres_database, method, status, paid, source):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(
            GuestPurchase(
                token='local-excluded',
                contact_type='email',
                contact_value='local@example.com',
                period_days=30,
                amount_kopeks=19900,
                payment_method=method,
                source=source,
                status=status,
                paid_at=datetime.now(UTC) if paid else None,
            )
        )
        await db.commit()
        assert await list_recent_pending_payments(db) == []
        found, total = await search_payments(db, SearchParams())
        assert total == 0 and found == []


async def test_cryptobot_payload_does_not_duplicate_landing_purchase(postgres_database):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=1001, language='ru', status='active', balance_kopeks=0))
        await db.flush()
        db.add(
            GuestPurchase(
                token='local-crypto',
                contact_type='email',
                contact_value='local@example.com',
                period_days=30,
                amount_kopeks=19900,
                payment_method='cryptobot',
                payment_id='local-crypto-id',
                source='landing',
                status='delivered',
                paid_at=datetime.now(UTC),
                user_id=1,
            )
        )
        db.add(
            CryptoBotPayment(
                user_id=1,
                invoice_id='local-crypto-id',
                amount='2',
                asset='USDT',
                status='paid',
                payload='{"purpose":"guest_purchase","purchase_token":"local-crypto"}',
            )
        )
        await db.commit()
        records = await list_recent_pending_payments(db)
        assert len(records) == 1 and records[0].source == 'landing'
        found, total = await search_payments(db, SearchParams(method_filter=PaymentMethod.CRYPTOBOT))
        assert total == 1 and found[0].source == 'landing'


async def test_confirmed_gift_replay_and_http_details_keep_source_identity(postgres_database, monkeypatch):
    monkeypatch.setattr('app.services.guest_purchase_service.notify_gift_claim_available', AsyncMock())
    monkeypatch.setattr('app.services.guest_purchase_service._create_nalogo_receipt_for_purchase', AsyncMock())
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        admin = User(id=1, telegram_id=1001, language='ru', status='active', balance_kopeks=50000)
        db.add(admin)
        await db.flush()
        db.add(
            GuestPurchase(
                id=1,
                token='local-claim-secret',
                contact_type='email',
                contact_value='local@example.com',
                period_days=30,
                amount_kopeks=19900,
                payment_method='platega_2',
                source='landing',
                status='pending',
                is_gift=True,
                buyer_user_id=1,
            )
        )
        db.add(
            PlategaPayment(
                id=1,
                user_id=1,
                amount_kopeks=50000,
                status='PENDING',
                is_paid=False,
                platega_transaction_id='unrelated-provider-invoice',
                correlation_id='unrelated',
                payment_method_code=2,
            )
        )
        await db.commit()
        for _ in range(2):
            assert (
                await try_fulfill_guest_purchase(
                    db,
                    metadata={'purpose': 'guest_purchase', 'purchase_token': 'local-claim-secret'},
                    payment_amount_kopeks=19900,
                    provider_payment_id='local-guest-invoice',
                    provider_name='platega',
                )
                is True
            )
        app = FastAPI()
        app.include_router(admin_payments.router)

        async def database_override():
            yield db

        app.dependency_overrides[get_cabinet_db] = database_override
        # Keep HTTP parsing/serialization real; supply an authorized actor without a live login.
        for endpoint in [admin_payments.get_pending_payment_details, admin_payments.check_payment_status]:
            dependency = inspect.signature(endpoint).parameters['admin'].default.dependency
            app.dependency_overrides[dependency] = lambda: admin
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            provider = await client.get('/admin/payments/platega/1')
            assert provider.status_code == 200
            assert provider.json()['identifier'] == 'unrelated-provider-invoice'
            landing = await client.get('/admin/payments/platega/1', params={'source': 'landing'})
            assert landing.status_code == 200
            assert landing.json()['identifier'] == 'local-guest-invoice'
            assert landing.json()['source'] == 'landing'
            assert landing.json()['amount_kopeks'] == 19900
            assert 'local-claim-secret' not in landing.text
            checked = await client.post('/admin/payments/platega/1/check', params={'source': 'landing'})
            assert checked.status_code == 200
            assert checked.json()['success'] is False
            assert checked.json()['payment']['source'] == 'landing'
            assert (await client.get('/admin/payments/platega/1', params={'source': 'invalid'})).status_code == 422
        records = await list_recent_pending_payments(db)
        assert len([p for p in records if p.source == 'landing']) == 1
        assert await db.scalar(select(func.count()).select_from(Transaction)) == 0
        await db.refresh(admin)
        assert admin.balance_kopeks == 50000


@pytest.mark.parametrize('view', ['list', 'search', 'stats'])
async def test_payment_confirmed_between_admin_queries_is_not_duplicated(postgres_database, monkeypatch, view):
    from app.services import landing_payment_records, payment_search_service

    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=1001, language='ru', status='active', balance_kopeks=0))
        await db.flush()
        db.add(
            GuestPurchase(
                token='local-race',
                contact_type='email',
                contact_value='local@example.com',
                period_days=30,
                amount_kopeks=19900,
                payment_method='platega_2',
                payment_id='local-race-invoice',
                source='landing',
                status='pending',
            )
        )
        db.add(
            PlategaPayment(
                user_id=1,
                amount_kopeks=19900,
                status='PENDING',
                is_paid=False,
                platega_transaction_id='local-race-invoice',
                correlation_id='local-race-correlation',
                payment_method_code=2,
                metadata_json={'purpose': 'guest_purchase', 'purchase_token': 'local-race'},
            )
        )
        await db.commit()
        original_fetch = landing_payment_records.fetch_landing_payments

        async def confirm_then_fetch(*args, **kwargs):
            # A separate real transaction commits after provider invoices were read.
            async with AsyncSession(db.bind) as payment_db:
                await payment_db.execute(
                    update(GuestPurchase)
                    .where(GuestPurchase.token == 'local-race')
                    .values(status='paid', paid_at=datetime.now(UTC))
                )
                await payment_db.commit()
            return await original_fetch(*args, **kwargs)

        target = landing_payment_records if view == 'list' else payment_search_service
        monkeypatch.setattr(target, 'fetch_landing_payments', confirm_then_fetch)
        if view == 'list':
            found = await list_recent_pending_payments(db)
            assert len(found) == 1 and found[0].source == 'landing'
        elif view == 'search':
            found, total = await search_payments(db, SearchParams())
            assert total == 1 and found[0].source == 'landing'
        else:
            stats = await search_payments_stats(db, SearchParams())
            assert stats.total == stats.paid == 1
            assert stats.pending == 0
