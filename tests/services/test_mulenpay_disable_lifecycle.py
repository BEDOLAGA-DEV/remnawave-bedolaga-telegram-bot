"""Регрессии жизненного цикла MulenPay при отключении новых платежей."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import PaymentMethod
from app.services import payment_verification_service as pvs
from app.services.mulenpay_service import MulenPayService
from app.services.payment_service import PaymentService


def _configure_mulenpay(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(settings, 'MULENPAY_ENABLED', enabled, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_API_KEY', 'api', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SECRET_KEY', 'secret', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_BASE_URL', 'https://mulenpay.test', raising=False)


def test_adapter_stays_configured_when_new_payments_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mulenpay(monkeypatch, enabled=False)

    service = MulenPayService()

    assert settings.is_mulenpay_configured() is True
    assert settings.is_mulenpay_enabled() is False
    assert service.is_configured is True


def test_payment_service_keeps_mulenpay_client_for_existing_payments(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mulenpay(monkeypatch, enabled=False)

    for method_name in (
        'is_yookassa_enabled',
        'is_cryptobot_enabled',
        'is_heleket_enabled',
        'is_pal24_enabled',
        'is_platega_enabled',
        'is_wata_enabled',
        'is_cloudpayments_enabled',
        'is_nalogo_enabled',
    ):
        monkeypatch.setattr(type(settings), method_name, lambda self: False, raising=False)

    created: list[object] = []

    class DummyMulenPayService:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr('app.services.payment_service.MulenPayService', DummyMulenPayService)

    service = PaymentService()

    assert created
    assert service.mulenpay_service is created[0]


@pytest.mark.anyio('asyncio')
async def test_create_mulenpay_payment_fails_closed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mulenpay(monkeypatch, enabled=False)

    provider = SimpleNamespace(create_payment=AsyncMock())
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    service.mulenpay_service = provider

    result = await service.create_mulenpay_payment(
        db=None,
        user_id=42,
        amount_kopeks=10000,
        description='Пополнение',
    )

    assert result is None
    provider.create_payment.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_adapter_create_payment_fails_closed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mulenpay(monkeypatch, enabled=False)

    service = MulenPayService()
    request = AsyncMock()
    monkeypatch.setattr(service, '_request', request)

    result = await service.create_payment(
        amount_kopeks=10000,
        description='Пополнение',
        uuid='mulen_test',
        items=[],
    )

    assert result is None
    request.assert_not_awaited()


def test_auto_verification_keeps_configured_mulenpay_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_mulenpay(monkeypatch, enabled=False)

    assert PaymentMethod.MULENPAY in pvs.get_enabled_auto_methods()
