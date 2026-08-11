import pytest
import structlog

from app.config import settings
from app.services.platega_service import PlategaService


def _configure_platega(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    values = {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'merchant',
        'PLATEGA_SECRET': 'secret',
        'PLATEGA_BASE_URL': 'https://app.platega.io',
        'PLATEGA_API_VERSION': 'v1',
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)


async def _captured_create_endpoint(monkeypatch: pytest.MonkeyPatch, service: PlategaService) -> str:
    captured: dict[str, str] = {}

    async def fake_request(method, endpoint, **kwargs):
        captured['method'] = method
        captured['endpoint'] = endpoint
        return {'transactionId': 'tx', 'status': 'PENDING'}

    monkeypatch.setattr(service, '_request', fake_request)
    await service.create_payment(payment_method=2, amount=100.0, currency='RUB')
    assert captured['method'] == 'POST'
    return captured['endpoint']


def test_sanitize_description_limits_utf8_bytes() -> None:
    original = 'Интернет-сервис - Пополнение баланса на 50 ₽ и ещё чуть-чуть'

    with structlog.testing.capture_logs() as logs:
        trimmed = PlategaService._sanitize_description(original, 64)

    assert len(trimmed.encode('utf-8')) <= 64
    assert trimmed != original
    assert any('trimmed' in entry.get('event', '') for entry in logs)


def test_sanitize_description_returns_clean_value() -> None:
    original = '  Обычное описание  '

    trimmed = PlategaService._sanitize_description(original, 64)

    assert trimmed == 'Обычное описание'
    assert len(trimmed.encode('utf-8')) <= 64


# --- API version selection (#2934) ---
# Platega v2 (POST /v2/transaction/process) отвечает полем `url`, v1 — `redirect`.
# У части мерчантов карточный flow (метод 11) в v1 отдаёт 400 «No available card
# cascades», поэтому версия create-эндпоинта настраивается через PLATEGA_API_VERSION.


async def test_create_payment_defaults_to_v1_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_platega(monkeypatch)

    service = PlategaService()

    assert service.api_version == 'v1'
    assert await _captured_create_endpoint(monkeypatch, service) == '/transaction/process'


async def test_create_payment_uses_v2_endpoint_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_platega(monkeypatch, PLATEGA_API_VERSION='v2')

    service = PlategaService()

    assert service.api_version == 'v2'
    assert await _captured_create_endpoint(monkeypatch, service) == '/v2/transaction/process'


async def test_base_url_version_suffix_forces_version_and_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обход из #2934 (PLATEGA_BASE_URL=…/v2) не должен собирать /v2/v2/… и не
    должен уводить неверсионированный статусный GET на /v2/transaction/{id}."""
    _configure_platega(monkeypatch, PLATEGA_BASE_URL='https://app.platega.io/v2', PLATEGA_API_VERSION='v1')

    service = PlategaService()

    assert service.base_url == 'https://app.platega.io'
    assert service.api_version == 'v2'
    assert await _captured_create_endpoint(monkeypatch, service) == '/v2/transaction/process'


async def test_get_transaction_stays_unversioned(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_platega(monkeypatch, PLATEGA_API_VERSION='v2')

    service = PlategaService()
    captured: dict[str, str] = {}

    async def fake_request(method, endpoint, **kwargs):
        captured['method'] = method
        captured['endpoint'] = endpoint
        return {'id': 'tx', 'status': 'PENDING'}

    monkeypatch.setattr(service, '_request', fake_request)
    await service.get_transaction('tx')

    assert captured == {'method': 'GET', 'endpoint': '/transaction/tx'}


def test_unknown_api_version_falls_back_to_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_platega(monkeypatch, PLATEGA_API_VERSION='v3')

    with structlog.testing.capture_logs() as logs:
        service = PlategaService()

    assert service.api_version == 'v1'
    assert any('PLATEGA_API_VERSION' in entry.get('event', '') for entry in logs)


# --- Redirect URL parsing (#2934) ---


def test_parse_redirect_url_accepts_v1_field() -> None:
    response = {'transactionId': 'tx', 'status': 'PENDING', 'redirect': 'https://pay.platega.io?id=tx'}

    assert PlategaService.parse_redirect_url(response) == 'https://pay.platega.io?id=tx'


def test_parse_redirect_url_accepts_v2_field() -> None:
    response = {'transactionId': 'tx', 'status': 'PENDING', 'url': 'https://pay.platega.io?id=tx'}

    assert PlategaService.parse_redirect_url(response) == 'https://pay.platega.io?id=tx'


def test_parse_redirect_url_missing_or_empty() -> None:
    assert PlategaService.parse_redirect_url(None) is None
    assert PlategaService.parse_redirect_url({}) is None
    assert PlategaService.parse_redirect_url({'redirect': '', 'url': ''}) is None


def test_base_url_version_suffix_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A manually appended suffix may be uppercase ('/V2'); it must still be stripped
    and treated as a forced version, not left to build a malformed '/V2/transaction/...'."""
    _configure_platega(monkeypatch, PLATEGA_BASE_URL='https://app.platega.io/V2', PLATEGA_API_VERSION='v1')

    service = PlategaService()

    assert service.base_url == 'https://app.platega.io'
    assert service.api_version == 'v2'


async def test_v2_url_field_reaches_returned_redirect_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """END-TO-END regression for #2934: a v2 create response carrying the link in
    ``url`` (not ``redirect``) must surface as ``redirect_url`` in the dict returned
    by the mixin — this pins the actual fix site (payment/platega.py), so reverting
    it to ``response.get('redirect')`` fails here even though the isolated
    parse_redirect_url tests stay green.
    """
    import types

    from app.services.payment import platega as platega_mixin

    _configure_platega(monkeypatch, PLATEGA_API_VERSION='v2')
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 1, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 10_000_000, raising=False)

    pay_link = 'https://pay.platega.io?id=tx'
    service = PlategaService()

    async def fake_request(method, endpoint, **kwargs):
        # v2 shape: link is under `url`, no `redirect` key at all
        return {'transactionId': 'tx', 'status': 'PENDING', 'url': pay_link}

    monkeypatch.setattr(service, '_request', fake_request)

    # Stub the DB persistence so no real session is needed; it returns an object with .id
    payment_service_module = platega_mixin.import_module('app.services.payment_service')

    async def fake_persist(db, **kwargs):
        return types.SimpleNamespace(id=123)

    monkeypatch.setattr(payment_service_module, 'create_platega_payment', fake_persist, raising=False)

    mixin = platega_mixin.PlategaPaymentMixin()
    mixin.platega_service = service

    result = await mixin.create_platega_payment(
        db=None,
        user_id=1,
        amount_kopeks=10_000,
        description='Пополнение',
        language='ru',
        payment_method_code=2,
    )

    assert result is not None
    assert result['redirect_url'] == pay_link
