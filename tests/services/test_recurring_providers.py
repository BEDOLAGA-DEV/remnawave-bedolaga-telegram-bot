"""Тесты абстракции рекуррентных провайдеров и встроенных реализаций."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


from app.config import settings
from app.services.payment.recurring import (
    enabled_providers,
    get_provider,
    is_any_recurring_enabled,
)
from app.services.payment.recurring.base import ChargeResult, RecurringProvider
from app.services.payment.recurring.etoplatezhi_provider import EtoPlatezhiRecurringProvider
from app.services.payment.recurring.yookassa_provider import YooKassaRecurringProvider


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_resolves_known_providers() -> None:
    assert isinstance(get_provider('yookassa'), YooKassaRecurringProvider)
    assert isinstance(get_provider('etoplatezhi'), EtoPlatezhiRecurringProvider)
    assert get_provider('unknown') is None


def test_enabled_providers_reflects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both flags are off, ``enabled_providers`` returns an empty list and
    ``is_any_recurring_enabled`` is False."""
    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_RECURRENT_ENABLED', False, raising=False)
    assert enabled_providers() == []
    assert is_any_recurring_enabled() is False


def test_enabled_providers_includes_etoplatezhi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_RECURRENT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_PROJECT_ID', 42, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_SECRET_KEY', 'secret', raising=False)

    enabled = enabled_providers()
    assert any(provider.name == 'etoplatezhi' for provider in enabled)
    assert is_any_recurring_enabled() is True


# ---------------------------------------------------------------------------
# YooKassa provider
# ---------------------------------------------------------------------------


@pytest.mark.anyio('asyncio')
async def test_yookassa_provider_delegates_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = YooKassaRecurringProvider()

    fake_service = SimpleNamespace(
        configured=True,
        create_autopayment=AsyncMock(return_value={'id': 'yk_1', 'status': 'succeeded', 'paid': True}),
    )
    provider._service = fake_service  # type: ignore[attr-defined]

    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', True, raising=False)

    result = await provider.charge(
        provider_token='pm_abc',
        amount_kopeks=29900,
        description='Top-up',
        metadata={'user_id': '1'},
        idempotency_key='idem-1',
        user_id=1,
    )

    fake_service.create_autopayment.assert_awaited_once()
    kwargs = fake_service.create_autopayment.await_args.kwargs
    assert kwargs['amount'] == 299.0
    assert kwargs['currency'] == 'RUB'
    assert kwargs['payment_method_id'] == 'pm_abc'
    assert kwargs['idempotence_key'] == 'idem-1'

    assert isinstance(result, ChargeResult)
    assert result.success is True
    assert result.provider_payment_id == 'yk_1'
    assert result.raw == {'id': 'yk_1', 'status': 'succeeded', 'paid': True}


@pytest.mark.anyio('asyncio')
async def test_yookassa_provider_returns_failure_when_service_returns_none() -> None:
    provider = YooKassaRecurringProvider()
    provider._service = SimpleNamespace(  # type: ignore[attr-defined]
        configured=True,
        create_autopayment=AsyncMock(return_value=None),
    )

    result = await provider.charge(
        provider_token='pm_abc',
        amount_kopeks=10000,
        description='Top-up',
        metadata={},
        idempotency_key='idem-2',
    )

    assert result.success is False
    assert 'returned None' in (result.error_message or '')


# ---------------------------------------------------------------------------
# EtoPlatezhi provider
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = ''

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    """Minimal stand-in for ``httpx.AsyncClient`` used in the EtoPlatezhi tests."""

    def __init__(self, response: _FakeResponse, *, raise_on_post: Exception | None = None):
        self._response = response
        self._raise = raise_on_post
        self.last_url: str | None = None
        self.last_json: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
        if self._raise:
            raise self._raise
        self.last_url = url
        self.last_json = json
        self.last_headers = headers
        return self._response


def _configure_etoplatezhi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ETOPLATEZHI_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_RECURRENT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_PROJECT_ID', 42, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_SECRET_KEY', 'secret-key', raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_CURRENCY', 'RUB', raising=False)


@pytest.mark.anyio('asyncio')
async def test_etoplatezhi_provider_builds_signed_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_etoplatezhi(monkeypatch)
    fake = _FakeAsyncClient(
        _FakeResponse(
            200,
            {
                'payment': {'id': '456789', 'type': 'recurring', 'status': 'success'},
                'operation': {'id': 100500, 'status': 'success'},
                'recurring': {'id': 1079},
            },
        )
    )

    import app.services.payment.recurring.etoplatezhi_provider as module

    monkeypatch.setattr(module, 'httpx', SimpleNamespace(AsyncClient=lambda *_a, **_kw: fake))

    provider = EtoPlatezhiRecurringProvider()
    result = await provider.charge(
        provider_token='1079',
        amount_kopeks=29900,
        description='Auto top-up',
        metadata={'user_telegram_id': '7777'},
        idempotency_key='auto-2026-05-19-001',
        user_id=1,
    )

    assert result.success is True
    assert result.provider_payment_id == '100500'
    assert fake.last_url == 'https://api.etoplatezhi.ru/v2/payment/card/recurring'
    assert fake.last_headers == {'Content-Type': 'application/json', 'Accept': 'application/json'}

    body = fake.last_json or {}
    assert body['general']['project_id'] == 42
    assert body['general']['payment_id'] == 'auto-2026-05-19-001'
    assert isinstance(body['general']['signature'], str) and body['general']['signature']
    assert body['customer'] == {'id': '7777'}
    assert body['payment'] == {'amount': 29900, 'currency': 'RUB', 'description': 'Auto top-up'}
    assert body['recurring'] == {'id': 1079}


@pytest.mark.anyio('asyncio')
async def test_etoplatezhi_provider_rejects_non_numeric_token() -> None:
    provider = EtoPlatezhiRecurringProvider()
    result = await provider.charge(
        provider_token='not-a-number',
        amount_kopeks=10000,
        description='Top-up',
        metadata={},
        idempotency_key='idem-3',
    )
    assert result.success is False
    assert result.error_code == 'invalid_token'


@pytest.mark.anyio('asyncio')
async def test_etoplatezhi_provider_handles_declined_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_etoplatezhi(monkeypatch)
    fake = _FakeAsyncClient(
        _FakeResponse(
            200,
            {
                'payment': {'id': '456789', 'status': 'decline'},
                'operation': {'id': 100501, 'status': 'decline'},
            },
        )
    )

    import app.services.payment.recurring.etoplatezhi_provider as module

    monkeypatch.setattr(module, 'httpx', SimpleNamespace(AsyncClient=lambda *_a, **_kw: fake))

    provider = EtoPlatezhiRecurringProvider()
    result = await provider.charge(
        provider_token='1079',
        amount_kopeks=10000,
        description='Top-up',
        metadata={},
        idempotency_key='idem-4',
    )
    assert result.success is False
    assert result.error_code == 'charge_declined'


@pytest.mark.anyio('asyncio')
async def test_etoplatezhi_provider_handles_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_etoplatezhi(monkeypatch)
    fake = _FakeAsyncClient(_FakeResponse(500, {'error': 'internal'}))

    import app.services.payment.recurring.etoplatezhi_provider as module

    monkeypatch.setattr(module, 'httpx', SimpleNamespace(AsyncClient=lambda *_a, **_kw: fake))

    provider = EtoPlatezhiRecurringProvider()
    result = await provider.charge(
        provider_token='1079',
        amount_kopeks=10000,
        description='Top-up',
        metadata={},
        idempotency_key='idem-5',
    )
    assert result.success is False
    assert (result.error_code or '').startswith('http_5')


# ---------------------------------------------------------------------------
# Base abstraction contract
# ---------------------------------------------------------------------------


def test_recurring_provider_is_abstract() -> None:
    with pytest.raises(TypeError):
        RecurringProvider()  # type: ignore[abstract]
