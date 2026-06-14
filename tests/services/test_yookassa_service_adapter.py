"""Тесты низкоуровневого сервиса YooKassaService."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from yookassa import Configuration  # type: ignore

import app.services.yookassa_service as yookassa_service_module
from app.config import settings
from app.services.yookassa_service import YooKassaService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


def _prepare_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', 'shop123', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', 'secret123', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_RETURN_URL', 'https://example.com/return', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_VAT_CODE', 1, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service', raising=False)


def test_init_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', '', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', '', raising=False)
    service = YooKassaService()
    assert service.configured is False
    assert service.return_url == 'https://t.me/'


@pytest.mark.anyio('asyncio')
async def test_create_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', None, raising=False)

    captured_calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                'id': 'yk_1',
                'status': 'pending',
                'paid': False,
                'confirmation': {'confirmation_url': 'https://yk/confirm'},
                'metadata': {'meta': 'value'},
                'amount': {'value': '140.00', 'currency': 'RUB'},
                'refundable': True,
                'created_at': '2024-01-01T12:00:00+00:00',
                'description': 'Desc',
                'test': False,
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            captured_calls.append({'url': url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(
        yookassa_service_module,
        'httpx',
        SimpleNamespace(AsyncClient=FakeAsyncClient, BasicAuth=lambda username, password: (username, password)),
        raising=False,
    )

    service = YooKassaService()
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    result = await service.create_payment(
        amount=140.0,
        currency='RUB',
        description='Пополнение',
        metadata={'order': '1'},
        receipt_email='user@example.com',
    )

    assert service.configured is True
    assert result is not None
    assert result['id'] == 'yk_1'
    assert result['confirmation_url'] == 'https://yk/confirm'
    assert result['amount_value'] == 140.0
    assert result['status'] == 'pending'
    assert captured_calls[0]['auth'] == ('shop123', 'secret123')
    assert captured_calls[0]['json']['amount']['value'] == '140.00'
    assert captured_calls[0]['json']['receipt']['items'][0]['amount']['value'] == '140.00'


@pytest.mark.anyio('asyncio')
async def test_create_payment_without_contacts(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', None, raising=False)

    service = YooKassaService()
    result = await service.create_payment(
        amount=10,
        currency='RUB',
        description='desc',
        metadata={},
    )
    assert result is not None
    assert result.get('error') is True


@pytest.mark.anyio('asyncio')
async def test_create_payment_returns_none_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_SHOP_ID', '', raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_SECRET_KEY', '', raising=False)
    service = YooKassaService()
    result = await service.create_payment(
        amount=10,
        currency='RUB',
        description='desc',
        metadata={},
    )
    assert result is None


@pytest.mark.anyio('asyncio')
async def test_create_sbp_payment_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    captured_calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                'id': 'sbp_001',
                'status': 'pending',
                'paid': False,
                'confirmation': {'confirmation_url': 'https://sbp/confirm'},
                'metadata': {'meta': 'value'},
                'amount': {'value': '200.00', 'currency': 'RUB'},
                'refundable': False,
                'created_at': '2024-02-01T09:00:00+00:00',
                'description': 'SBP payment',
                'test': True,
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            captured_calls.append({'url': url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(
        yookassa_service_module,
        'httpx',
        SimpleNamespace(AsyncClient=FakeAsyncClient, BasicAuth=lambda username, password: (username, password)),
        raising=False,
    )

    service = YooKassaService()
    result = await service.create_sbp_payment(
        amount=200.0,
        currency='rub',
        description='Оплата',
        metadata={'type': 'sbp'},
        receipt_phone='+70000000000',
    )

    assert result is not None
    assert result['id'] == 'sbp_001'
    assert result['confirmation_url'] == 'https://sbp/confirm'
    assert result['status'] == 'pending'
    assert captured_calls[0]['json']['payment_method_data'] == {'type': 'sbp'}


@pytest.mark.anyio('asyncio')
async def test_create_payment_uses_scoped_basic_auth_without_global_sdk_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    captured_calls: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                'id': 'yk_http_1',
                'status': 'pending',
                'paid': False,
                'confirmation': {'confirmation_url': 'https://yk/confirm'},
                'metadata': {'scope': 'bot'},
                'amount': {'value': '140.00', 'currency': 'RUB'},
                'refundable': True,
                'created_at': '2024-01-01T12:00:00Z',
                'description': 'Пополнение',
                'test': False,
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            captured_calls.append({'url': url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(
        yookassa_service_module,
        'httpx',
        SimpleNamespace(AsyncClient=FakeAsyncClient, BasicAuth=lambda username, password: (username, password)),
        raising=False,
    )

    service = YooKassaService(shop_id='bot-shop', secret_key='bot-secret')
    result = await service.create_payment(
        amount=140.0,
        currency='RUB',
        description='Пополнение',
        metadata={'scope': 'bot'},
    )

    assert result is not None
    assert result['id'] == 'yk_http_1'
    assert captured_calls[0]['auth'] == ('bot-shop', 'bot-secret')
    assert captured_calls[0]['url'].endswith('/payments')


@pytest.mark.anyio('asyncio')
async def test_scoped_clients_keep_basic_auth_per_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    captured_auth: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, payment_id: str) -> None:
            self.payment_id = payment_id

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                'id': self.payment_id,
                'status': 'pending',
                'paid': False,
                'confirmation': {'confirmation_url': f'https://yk/{self.payment_id}'},
                'metadata': {},
                'amount': {'value': '10.00', 'currency': 'RUB'},
                'refundable': False,
                'created_at': '2024-01-01T12:00:00Z',
                'description': 'desc',
                'test': False,
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            auth = kwargs['auth']
            captured_auth.append(auth)
            return FakeResponse(f'payment_{len(captured_auth)}')

    monkeypatch.setattr(
        yookassa_service_module,
        'httpx',
        SimpleNamespace(AsyncClient=FakeAsyncClient, BasicAuth=lambda username, password: (username, password)),
        raising=False,
    )

    bot_service = YooKassaService(shop_id='bot-shop', secret_key='bot-secret')
    cabinet_service = YooKassaService(shop_id='cabinet-shop', secret_key='cabinet-secret')

    await bot_service.create_payment(amount=10, currency='RUB', description='desc', metadata={})
    await cabinet_service.create_payment(amount=10, currency='RUB', description='desc', metadata={})

    assert captured_auth == [
        ('bot-shop', 'bot-secret'),
        ('cabinet-shop', 'cabinet-secret'),
    ]
