"""Тесты низкоуровневого сервиса YooKassaService."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from yookassa import Configuration, Payment as YooKassaPayment  # type: ignore

from app.config import settings
from app.services.yookassa_service import YooKassaService


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class DummyLoop:
    async def run_in_executor(self, _executor, func):
        return func()


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
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop, raising=False)

    captured_config: dict[str, tuple[str, str]] = {}

    def fake_configure(shop_id: str, secret_key: str) -> None:
        captured_config['values'] = (shop_id, secret_key)

    monkeypatch.setattr(Configuration, 'configure', fake_configure, raising=False)

    response_obj = SimpleNamespace(
        id='yk_1',
        status='pending',
        paid=False,
        confirmation=SimpleNamespace(confirmation_url='https://yk/confirm'),
        metadata={'meta': 'value'},
        amount=SimpleNamespace(value='140.00', currency='RUB'),
        refundable=True,
        created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        description='Desc',
        test=False,
    )

    monkeypatch.setattr(
        YooKassaPayment,
        'create',
        staticmethod(lambda payload, key: response_obj),
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
    assert captured_config['values'] == ('shop123', 'secret123')
    assert result is not None
    assert result['id'] == 'yk_1'
    assert result['confirmation_url'] == 'https://yk/confirm'
    assert result['amount_value'] == 140.0
    assert result['status'] == 'pending'


@pytest.mark.anyio('asyncio')
async def test_create_payment_without_contacts(monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_config(monkeypatch)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', None, raising=False)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop, raising=False)
    monkeypatch.setattr(
        YooKassaPayment,
        'create',
        staticmethod(lambda payload, key: SimpleNamespace()),
        raising=False,
    )

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
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop, raising=False)
    monkeypatch.setattr(Configuration, 'configure', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', 'fallback@example.com', raising=False)

    response_obj = SimpleNamespace(
        id='sbp_001',
        status='pending',
        paid=False,
        confirmation=SimpleNamespace(confirmation_url='https://sbp/confirm'),
        metadata={'meta': 'value'},
        amount=SimpleNamespace(value='200.00', currency='RUB'),
        refundable=False,
        created_at=datetime(2024, 2, 1, 9, 0, 0, tzinfo=UTC),
        description='SBP payment',
        test=True,
    )

    monkeypatch.setattr(
        YooKassaPayment,
        'create',
        staticmethod(lambda payload, key: response_obj),
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
