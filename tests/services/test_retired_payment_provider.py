"""Removing a gateway must not revive it from old settings or erase revenue."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings, settings
from app.database.models import Base, PaymentMethodConfig
from app.services import payment_method_config_service as config_service
from app.webserver.payments import create_payment_router
from tests.fixtures.sqlite_memory import memory_session


def test_removed_provider_is_not_offered_even_with_old_settings(monkeypatch):
    monkeypatch.setattr(Settings, 'is_paypear_enabled', lambda self: True, raising=False)
    assert 'paypear' not in config_service._get_method_defaults()
    assert 'paypear' not in config_service.DEFAULT_METHOD_ORDER


@pytest.mark.asyncio
async def test_removed_provider_has_no_webhook_but_neighbor_still_does(monkeypatch):
    monkeypatch.setattr(Settings, 'is_paypear_configured', lambda self: True, raising=False)
    monkeypatch.setattr(Settings, 'PAYPEAR_WEBHOOK_PATH', '/paypear-webhook', raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_API_KEY', 'test-key')
    app = FastAPI()
    router = create_payment_router(SimpleNamespace(), SimpleNamespace())
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/paypear-webhook', json={})).status_code == 404
    assert settings.TRIBUTE_WEBHOOK_PATH in {route.path for route in router.routes}


@pytest.mark.asyncio
async def test_old_backup_config_is_hidden_without_hiding_other_providers(monkeypatch):
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                PaymentMethodConfig(method_id='paypear', is_enabled=True),
                PaymentMethodConfig(method_id='rollypay', is_enabled=True),
                PaymentMethodConfig(method_id='severpay', is_enabled=True),
            ]
        )
        await db.commit()
        assert {config.method_id for config in await config_service.get_all_configs(db)} == {'rollypay', 'severpay'}
        assert await config_service.get_config_by_method_id(db, 'paypear') is None


@pytest.mark.asyncio
async def test_retired_payments_still_count_as_revenue(monkeypatch):
    from sqlalchemy import func, select

    from app.database.crud.transaction import REAL_PAYMENT_METHODS
    from app.database.models import Transaction, User

    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=12345))
        await db.flush()
        for method, amount in [('paypear', 12300), ('rollypay', 45600), ('manual', 10000), ('balance', -20000)]:
            db.add(
                Transaction(user_id=1, type='deposit', payment_method=method, amount_kopeks=amount, is_completed=True)
            )
        await db.commit()
        revenue = await db.scalar(
            select(func.sum(Transaction.amount_kopeks)).where(Transaction.payment_method.in_(REAL_PAYMENT_METHODS))
        )
        assert revenue == 57900


@pytest.mark.asyncio
async def test_old_telegram_selection_cannot_create_payment():
    from app.handlers.balance.main import route_payment_by_method

    assert await route_payment_by_method(None, None, 10000, None, 'paypear') is False


@pytest.mark.asyncio
async def test_old_landing_selection_cannot_create_payment():
    from app.services.payment_service import PaymentService

    service = object.__new__(PaymentService)
    assert (
        await service.create_guest_payment(
            None,
            amount_kopeks=10000,
            payment_method='paypear',
            description='fixture',
            purchase_token='fixture',
            return_url='https://cabinet.example/return',
        )
        is None
    )
