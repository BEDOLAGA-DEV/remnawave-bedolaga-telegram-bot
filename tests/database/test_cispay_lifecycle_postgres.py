"""Real local HTTP CisPay API → signed FastAPI callback → PostgreSQL."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from fastapi import FastAPI
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, CisPayPayment, PaymentMethodConfig, Transaction, User
from app.handlers.balance import cispay as telegram_cispay
from app.services.payment_service import PaymentService
from app.webserver import payments
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('method', ['CARD', 'SBP', 'CRYPTO'])
@pytest.mark.parametrize('sandbox', [False, True])
@pytest.mark.parametrize('telegram_checkout', [False, True])
async def test_real_create_early_signed_callback_and_replay(
    postgres_database, monkeypatch, unused_tcp_port, method, sandbox, telegram_checkout
):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'masked-shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'synthetic-secret')
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_BASE_URL', f'http://127.0.0.1:{unused_tcp_port}')
    service = PaymentService()
    monkeypatch.setattr(service, '_after_cispay_payment', AsyncMock())
    callback_app = FastAPI()
    callback_app.include_router(payments.create_payment_router(SimpleNamespace(), service))
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, callback_db):
        db.add(User(id=1, telegram_id=1, balance_kopeks=100))
        if telegram_checkout:
            # Existing installation: bot ENV enabled, old cabinet record disabled.
            db.add(PaymentMethodConfig(method_id='cispay', is_enabled=False, sub_options={'card': True, 'sbp': True}))
        await db.commit()

        async def get_db():
            yield callback_db

        monkeypatch.setattr(payments, 'get_db', get_db)
        state = {}
        get_requests = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=callback_app), base_url='http://test'
        ) as callback:

            async def send(event, *, valid_signature=True):
                body = json.dumps(event, separators=(',', ':')).encode()
                signature = (
                    hmac.new(b'synthetic-secret', body, hashlib.sha256).hexdigest() if valid_signature else 'invalid'
                )
                return await callback.post(
                    settings.CISPAY_WEBHOOK_PATH, content=body, headers={'X-Signature': signature}
                )

            def assert_auth(request):
                assert request.headers['X-Shop-ID'] == 'masked-shop'
                assert request.headers['X-Api-Key'] == 'synthetic-secret'

            async def capabilities(request):
                assert_auth(request)
                return web.json_response(
                    {
                        'store_id': 'masked-shop',
                        'is_active': True,
                        'payment_methods': [{'payment_method': method, 'is_active': True}],
                    }
                )

            async def status(request):
                assert_auth(request)
                # Early callback finds a committed order before create response binds provider id.
                assert (
                    request.query.get('order_id') == state['event']['order_id']
                    or request.query.get('payment_id') == 'masked-invoice'
                )
                get_requests.append(dict(request.query))
                remote = {key: value for key, value in state['event'].items() if key != 'store_id'}
                if method == 'CRYPTO':
                    remote.update(source_amount=10001, source_currency='RUB')
                remote['expires_in_seconds'] = 7200
                return web.json_response(remote)

            async def create(request):
                assert_auth(request)
                data = await request.json()
                assert data['currency'] == 'RUB' and data['amount'] == 10001
                assert data['payment_method'] == method
                event = {
                    'id': 'masked-invoice',
                    'order_id': data['order_id'],
                    'store_id': 'masked-shop',
                    'status': 'PAID',
                    'payment_method': method,
                    'currency': 'USD' if method == 'CRYPTO' else 'RUB',
                    'amount': 111 if method == 'CRYPTO' else 10001,
                    'is_sandbox': sandbox,
                }
                state['event'] = event
                assert (await send(event, valid_signature=False)).status_code == 400
                response = await send(event)
                assert response.status_code == 200, response.text
                remote = {
                    'id': 'masked-invoice',
                    'order_id': data['order_id'],
                    'status': 'PENDING',
                    'amount': event['amount'],
                    'charged_amount': event['amount'],
                    'payment_url': 'https://pay.example/invoice',
                    'created_at': '2026-10-26T10:00:00Z',
                }
                if method == 'CRYPTO':
                    remote.update(source_amount=10001, source_currency='RUB')
                return web.json_response(remote, status=201)

            app = web.Application()
            app.router.add_get('/store/capabilities', capabilities)
            app.router.add_get('/payments/status', status)
            app.router.add_post('/payments', create)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
            try:
                if telegram_checkout:
                    # Same user in the cabinet cannot bypass the saved disable.
                    assert (
                        await service.create_cispay_payment(
                            db, user_id=1, amount_kopeks=10001, payment_method_type=method.lower()
                        )
                        is None
                    )
                    monkeypatch.setattr(telegram_cispay, 'PaymentService', lambda: service)
                    user = await db.get(User, 1)
                    fsm = AsyncMock()
                    fsm.get_data.return_value = {'payment_method': f'cispay_{method.lower()}'}
                    telegram_callback = AsyncMock()
                    await telegram_cispay._start_cispay_topup_impl(
                        telegram_callback, user, fsm, f'cispay_{method.lower()}', db
                    )
                    fsm.set_state.assert_awaited_once()
                    message = AsyncMock()
                    await telegram_cispay.process_cispay_payment_amount(message, user, db, 10001, fsm)
                    buttons = message.answer.await_args.kwargs['reply_markup'].inline_keyboard
                    assert buttons[0][0].url == 'https://pay.example/invoice'
                    saved = (await db.scalars(select(PaymentMethodConfig))).one()
                    assert saved.is_enabled is False
                    assert saved.sub_options == {'card': True, 'sbp': True}
                else:
                    result = await service.create_cispay_payment(
                        db, user_id=1, amount_kopeks=10001, payment_method_type=method.lower()
                    )
                    assert result
                assert (await send(state['event'])).status_code == 200
                assert (await send(state['event'])).status_code == 200
                payment = (await db.scalars(select(CisPayPayment))).one()
                assert payment.is_paid and payment.is_sandbox == sandbox
                assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == (100 if sandbox else 10101)
                assert len((await db.scalars(select(Transaction))).all()) == (0 if sandbox else 1)
                assert service._after_cispay_payment.await_count == (0 if sandbox else 1)
                assert len(get_requests) == (1 if method == 'CRYPTO' else 0)
                if method == 'CRYPTO':
                    assert payment.charged_amount_kopeks is None
            finally:
                await runner.cleanup()
