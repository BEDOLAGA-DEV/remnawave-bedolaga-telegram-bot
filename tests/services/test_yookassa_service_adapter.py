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


@pytest.mark.anyio('asyncio')
@pytest.mark.parametrize('card_kind', ['dict', 'object', 'partial', 'empty', 'none', 'absent'])
async def test_payment_info_card_representation_does_not_hide_payment(monkeypatch, card_kind):
    _prepare_config(monkeypatch)
    monkeypatch.setattr(asyncio, 'get_running_loop', DummyLoop)
    fields = {'first6': '555555', 'last4': '4444', 'card_type': 'Mir', 'expiry_month': '01', 'expiry_year': '2030'}
    card = fields.copy()
    if card_kind == 'object':
        card = SimpleNamespace(**fields)
    elif card_kind == 'partial':
        card = {'last4': '4444'}
    elif card_kind == 'empty':
        card = {}
    elif card_kind in {'none', 'absent'}:
        card = None
    method = SimpleNamespace(type='bank_card', id='method-example', saved=True)
    if card_kind != 'absent':
        method.card = card
    response = SimpleNamespace(
        id='payment-example',
        status='succeeded',
        paid=True,
        amount=SimpleNamespace(value='140.00', currency='RUB'),
        metadata={'user_id': '1'},
        description='Test payment',
        refundable=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        payment_method=method,
        test=True,
    )
    monkeypatch.setattr(YooKassaPayment, 'find_one', staticmethod(lambda _: response), raising=False)
    result = await YooKassaService().get_payment_info('payment-example')
    assert result is not None
    assert result['status'] == 'succeeded'
    assert result['paid'] is True
    assert result['amount_value'] == 140.0
    assert result['payment_method_saved'] is True
    if card_kind in {'none', 'empty', 'absent'}:
        assert result['payment_method_card'] is None
    elif card_kind == 'partial':
        assert result['payment_method_card'] == {key: '4444' if key == 'last4' else None for key in fields}
    else:
        assert result['payment_method_card'] == fields


def test_real_sdk_payment_models_are_supported():
    """Use a fresh interpreter: conftest replaces the SDK with a test stub."""
    import subprocess
    import textwrap

    script = textwrap.dedent("""
        import asyncio
        from yookassa import Payment
        from yookassa.domain.response.payment_response import PaymentResponse
        from app.services.yookassa_service import YooKassaService

        async def check():
            service = YooKassaService.__new__(YooKassaService)
            service.configured = True
            for method in ('bank_card', 'sberbank', 'tinkoff_bank', 'alfa_pay', 'sbp'):
                data = {
                    'id': 'payment-example', 'status': 'succeeded', 'paid': True,
                    'amount': {'value': '140.00', 'currency': 'RUB'},
                    'created_at': '2026-01-01T00:00:00Z', 'refundable': True,
                    'payment_method': {'type': method, 'id': 'method-example', 'saved': False},
                }
                if method != 'sbp':
                    data['payment_method']['card'] = {
                        'last4': '4444', 'expiry_month': '01', 'expiry_year': '2030', 'card_type': 'Mir',
                    }
                response = PaymentResponse(data)
                if method == 'alfa_pay':
                    assert isinstance(response.payment_method.card, dict)
                Payment.find_one = staticmethod(lambda _: response)
                result = await service.get_payment_info('payment-example')
                assert result is not None, method
                assert result['status'] == 'succeeded' and result['paid'] is True
                assert result['amount_value'] == 140.0
                if method == 'sbp':
                    assert result['payment_method_card'] is None
                else:
                    assert result['payment_method_card']['first6'] is None
                    assert result['payment_method_card']['last4'] == '4444'
        asyncio.run(check())
    """)
    # Fixed local test code only; no external input or network calls.
    result = subprocess.run(  # noqa: S603
        [sys.executable, '-c', script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
