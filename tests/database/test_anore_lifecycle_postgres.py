"""Real HTTP provider -> signed webhook -> PostgreSQL, including early callbacks."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from fastapi import FastAPI
from sqlalchemy import select

from app.config import settings
from app.database.models import AnorePayment, Base, Transaction, User
from app.services.payment_service import PaymentService
from app.webserver import payments
from app.webserver.anore import register_anore_webhook
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
@pytest.mark.parametrize('sandbox', [False, True])
async def test_real_signed_early_payment_and_replay(postgres_database, monkeypatch, unused_tcp_port, sandbox):
    monkeypatch.setattr(settings, 'ANORE_ENABLED', True)
    monkeypatch.setattr(settings, 'ANORE_API_KEY', 'an_test_example' if sandbox else 'an_live_example')
    monkeypatch.setattr(settings, 'ANORE_SECRET_KEY', 'synthetic-secret')
    monkeypatch.setattr(settings, 'ANORE_SHOP_ID', 7)
    monkeypatch.setattr(settings, 'ANORE_API_URL', f'http://127.0.0.1:{unused_tcp_port}/v1')
    service = PaymentService()
    monkeypatch.setattr(service, '_after_anore_payment', AsyncMock())
    webhook_app = FastAPI()
    register_anore_webhook(webhook_app, service)
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, callback_db):
        db.add(User(id=1, telegram_id=1, balance_kopeks=100))
        await db.commit()

        async def get_db():
            yield callback_db

        monkeypatch.setattr(payments, 'get_db', get_db)
        received = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=webhook_app), base_url='http://test'
        ) as callback:

            async def provider(request):
                raw = await request.read()
                assert (
                    request.headers['Anore-Signature'] == hmac.new(b'synthetic-secret', raw, hashlib.sha256).hexdigest()
                )
                data = json.loads(raw)
                assert data['amount'] == 100.01 and data['shopId'] == 7
                assert data['methods'] == ['card']
                event = {
                    'event': 'payment.succeeded',
                    'id': 'invoice-real-http',
                    'orderId': data['orderId'],
                    'amount': data['amount'],
                    'rubAmount': data['amount'],
                    'currency': 'rub',
                    'status': 'paid',
                }
                received.append(event)
                body = json.dumps(event).encode()
                headers = {
                    'Anore-Signature': hmac.new(b'synthetic-secret', body, hashlib.sha256).hexdigest(),
                    'Anore-Event': 'payment.succeeded',
                    'Anore-Delivery-Id': 'early',
                }
                response = await callback.post('/anore-webhook', content=body, headers=headers)
                assert response.status_code == 200, response.text
                return web.json_response(
                    {
                        **event,
                        'success': True,
                        'status': 'new',
                        'paymentUrl': 'https://pay.example.com/invoice',
                        'expiresIn': 14400,
                    }
                )

            app = web.Application()
            app.router.add_post('/v1/payments', provider)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
            try:
                result = await service.create_anore_payment(
                    db, user_id=1, amount_kopeks=10001, payment_method_type='card'
                )
                assert result and result['status'] == 'paid'
                for delivery in ('early', 'retry-another-id'):
                    body = json.dumps(received[0]).encode()
                    headers = {
                        'Anore-Signature': hmac.new(b'synthetic-secret', body, hashlib.sha256).hexdigest(),
                        'Anore-Event': 'payment.succeeded',
                        'Anore-Delivery-Id': delivery,
                    }
                    assert (await callback.post('/anore-webhook', content=body, headers=headers)).status_code == 200
                assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == (100 if sandbox else 10101)
                assert len((await db.scalars(select(Transaction))).all()) == (0 if sandbox else 1)
                payment = (await db.scalars(select(AnorePayment))).one()
                assert payment.is_test == sandbox and payment.is_paid
                assert service._after_anore_payment.await_count == (0 if sandbox else 1)
            finally:
                await runner.cleanup()
