"""Antilopay v1: real local HTTP, signed callbacks, and PostgreSQL ledger."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cabinet.routes import balance
from app.cabinet.schemas.balance import PaymentMethodResponse
from app.config import settings
from app.database.models import AntilopayPayment, Base, Transaction, User
from app.handlers.balance import antilopay as telegram_antilopay
from app.services import antilopay_service as client_module
from app.services.payment_service import PaymentService
from app.webserver import payments
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('channel', ['cabinet', 'telegram'])
@pytest.mark.parametrize('delivery', ['early', 'poll', 'concurrent'])
@pytest.mark.parametrize('method', ['card', 'sbp', 'sberpay'])
async def test_checkout_callback_and_recovery(
    postgres_database, monkeypatch, unused_tcp_port, delivery, method, channel
):
    key = RSA.generate(2048)
    for name, value in {
        'ANTILOPAY_ENABLED': True,
        'ANTILOPAY_SECRET_ID': 'masked-secret',
        'ANTILOPAY_PROJECT_ID': 'masked-project',
        'ANTILOPAY_PRIVATE_KEY': base64.b64encode(key.export_key()).decode(),
        'ANTILOPAY_PUBLIC_KEY': base64.b64encode(key.public_key().export_key()).decode(),
        'ANTILOPAY_CURRENCY': 'rub',
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(client_module, 'API_BASE_URL', f'http://127.0.0.1:{unused_tcp_port}/api/v1')
    service = PaymentService()
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
    monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', AsyncMock())
    callback_app = FastAPI()
    callback_app.include_router(payments.create_payment_router(SimpleNamespace(), service))
    async with postgres_sessions(postgres_database, list(Base.metadata.sorted_tables)) as (db, callback_db):
        db.add(User(id=1, email='user@example.com', balance_kopeks=100))
        await db.commit()

        async def get_db():
            yield callback_db

        monkeypatch.setattr(payments, 'get_db', get_db)
        state = {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=callback_app), base_url='http://test'
        ) as callback:

            async def send(event, valid=True):
                body = json.dumps(event, separators=(',', ':')).encode()
                signature = base64.b64encode(pkcs1_15.new(key).sign(SHA256.new(body))).decode()
                return await callback.post(
                    settings.ANTILOPAY_WEBHOOK_PATH,
                    content=body,
                    headers={
                        'X-Apay-Callback': signature if valid else 'invalid',
                        'X-Apay-Callback-Version': '1',
                    },
                )

            async def request_body(request):
                body = await request.read()
                pkcs1_15.new(key.public_key()).verify(
                    SHA256.new(body), base64.b64decode(request.headers['X-Apay-Sign'])
                )
                assert request.headers['X-Apay-Sign-Version'] == '1'
                return json.loads(body)

            async def create(request):
                body = await request_body(request)
                assert body['currency'] == 'RUB'
                assert body['customer'] == {'email': 'user@example.com'}
                assert body['prefer_methods'] == [{'card': 'CARD_RU', 'sbp': 'SBP', 'sberpay': 'SBER_PAY'}[method]]
                state['event'] = {
                    'type': 'payment',
                    'payment_id': 'masked-invoice',
                    'order_id': body['order_id'],
                    'original_amount': 100.01,
                    'amount': 95.0,
                    'currency': 'RUB',
                    'status': 'SUCCESS',
                }
                if delivery == 'early':
                    assert (await send(state['event'], valid=False)).status_code == 400
                    for invalid in [{'original_amount': None}, {'original_amount': 100.0}, {'currency': 'USD'}]:
                        assert (await send({**state['event'], **invalid})).status_code == 200
                        assert (await callback_db.get(User, 1, populate_existing=True)).balance_kopeks == 100
                        await callback_db.rollback()
                    assert (await send(state['event'])).status_code == 200
                return web.json_response(
                    {'code': 0, 'payment_id': 'masked-invoice', 'payment_url': 'https://pay.example/invoice'}
                )

            async def check(request):
                body = await request_body(request)
                assert body['order_id'] == state['event']['order_id']
                return web.json_response({'code': 0, **state['event']})

            api = web.Application()
            api.router.add_post('/api/v1/payment/create', create)
            api.router.add_post('/api/v1/payment/check', check)
            runner = web.AppRunner(api)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', unused_tcp_port).start()
            try:
                if channel == 'cabinet':
                    cabinet = FastAPI()
                    cabinet.include_router(balance.router)
                    cabinet.dependency_overrides[balance.get_cabinet_db] = lambda: db
                    user = await db.get(User, 1)
                    cabinet.dependency_overrides[balance.get_current_cabinet_user] = lambda: user
                    monkeypatch.setattr(balance, 'PaymentService', lambda: service)
                    monkeypatch.setattr(
                        balance,
                        'get_payment_methods',
                        AsyncMock(
                            return_value=[
                                PaymentMethodResponse(
                                    id='antilopay',
                                    name='Antilopay',
                                    min_amount_kopeks=10000,
                                    max_amount_kopeks=1000000,
                                    options=[{'id': method}],
                                )
                            ]
                        ),
                    )
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=cabinet), base_url='http://test'
                    ) as web_client:
                        response = await web_client.post(
                            '/balance/topup',
                            json={
                                'amount_kopeks': 10001,
                                'payment_method': 'antilopay',
                                'payment_option': method,
                                'customer_email': 'user@example.com',
                            },
                        )
                        assert response.status_code == 200, response.text
                        assert response.json()['payment_url'] == 'https://pay.example/invoice'
                else:
                    monkeypatch.setattr(telegram_antilopay, 'PaymentService', lambda: service)
                    user = await db.get(User, 1)
                    message = AsyncMock()
                    await telegram_antilopay._create_antilopay_payment_and_respond(
                        message,
                        user,
                        db,
                        10001,
                        payment_method_type=method,
                    )
                    buttons = message.answer.await_args.kwargs['reply_markup'].inline_keyboard
                    assert buttons[0][0].url == 'https://pay.example/invoice'
                if delivery == 'concurrent':
                    factory = async_sessionmaker(db.bind, expire_on_commit=False)

                    async def settle():
                        async with factory() as independent_db:
                            return await service.process_antilopay_callback(independent_db, state['event'])

                    assert all(await asyncio.gather(settle(), settle()))
                if delivery == 'poll':
                    from app.database.models import PaymentMethod
                    from app.services.payment_verification_service import run_manual_check

                    pending = (await db.scalars(select(AntilopayPayment))).one()
                    checked = await run_manual_check(db, PaymentMethod.ANTILOPAY, pending.id, service)
                    assert checked and checked.is_paid
                payment = (await db.scalars(select(AntilopayPayment).execution_options(populate_existing=True))).one()
                assert payment.is_paid
                await db.commit()
                assert (await send(state['event'])).status_code == 200
                assert (await send(state['event'])).status_code == 200
                assert (await db.get(User, 1, populate_existing=True)).balance_kopeks == 10101
                assert len((await db.scalars(select(Transaction))).all()) == 1
            finally:
                await client_module.antilopay_service.close()
                await runner.cleanup()
