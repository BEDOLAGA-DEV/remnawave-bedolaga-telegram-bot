"""Tests for the EtoPlatezhi СБП (SBP) enablement.

Background
----------
СБП (Faster Payments / sbp-qr) was enabled as a payment sub-method for the
EtoPlatezhi gateway. Three layers changed and are pinned here:

  1. ``payment_method_config_service._get_method_defaults()`` — the
     ``etoplatezhi`` config now offers an ``sbp`` sub-option, so the cabinet
     surfaces "СБП" as a selectable method.
  2. ``EtoplatezhiPaymentMixin.create_etoplatezhi_payment`` — maps the chosen
     ``payment_method_type`` to an EtoPlatezhi force-method code via
     ``force_method_map`` (``sbp`` → ``sbp-qr``, ``card`` → ``card-partner``)
     and passes it to ``build_payment_url(force_payment_method=...)``. Unknown
     / None types must force nothing (None) so the user keeps the full method
     picker.
  3. ``EtoplatezhiService.build_payment_url(force_payment_method=...)`` — emits
     ``force_payment_method=<value>`` as a signed top-level URL query param.

These tests assert observable outputs (config dict, the kwarg passed to
``build_payment_url``, and the generated URL query string). No real network or
DB is touched.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.services import payment_method_config_service as cfg_service
from app.services.etoplatezhi_service import EtoplatezhiService
from app.services.payment import etoplatezhi as etp_payment


# --------------------------------------------------------------------------- #
# (a) СБП is offered as an EtoPlatezhi sub-option
# --------------------------------------------------------------------------- #
def test_etoplatezhi_config_offers_sbp_sub_option() -> None:
    """The etoplatezhi default config must expose an 'sbp' sub-option named
    'СБП' so the cabinet lets the user pick Faster Payments."""
    defaults = cfg_service._get_method_defaults()
    options = defaults['etoplatezhi']['available_sub_options']

    assert options, 'etoplatezhi must have sub-options'
    ids = [opt['id'] for opt in options]
    assert 'sbp' in ids, f'expected sbp in etoplatezhi sub-options, got {ids}'

    sbp = next(opt for opt in options if opt['id'] == 'sbp')
    assert sbp['name'] == 'СБП'
    # СБП is listed first (primary method on the picker).
    assert ids[0] == 'sbp', f'expected sbp first, got {ids}'


# --------------------------------------------------------------------------- #
# (b) payment_method_type → force_payment_method mapping
# --------------------------------------------------------------------------- #
def _enable_etoplatezhi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), 'is_etoplatezhi_enabled', lambda self: True, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_PROJECT_ID', 555, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_SECRET_KEY', 'sekret', raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_CURRENCY', 'RUB', raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_MAX_AMOUNT_KOPEKS', 10000000, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_PAYMENT_LIFETIME_MINUTES', 60, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_WEBHOOK_PATH', '/etoplatezhi-webhook', raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_RETURN_URL', 'https://example.com/return', raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_RECURRENT_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'ETOPLATEZHI_RECURRENT_REQUIRED', False, raising=False)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://example.com', raising=False)


async def _call_create(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payment_method_type: str | None,
) -> MagicMock:
    """Run create_etoplatezhi_payment with all I/O mocked; return the mocked
    build_payment_url so the caller can inspect its kwargs."""
    _enable_etoplatezhi(monkeypatch)

    build_mock = MagicMock(return_value='https://paymentpage.etoplatezhi.ru/payment?stub=1')
    monkeypatch.setattr(etp_payment.etoplatezhi_service, 'build_payment_url', build_mock)

    # Stub the two import_module(...) targets used inside the method:
    #   app.services.payment_service  -> get_user_by_id
    #   app.database.crud.etoplatezhi -> create_etoplatezhi_payment
    fake_payment_service = types.ModuleType('app.services.payment_service')
    fake_payment_service.get_user_by_id = AsyncMock(
        return_value=types.SimpleNamespace(telegram_id=123456),
    )
    fake_crud = types.ModuleType('app.database.crud.etoplatezhi')
    fake_crud.create_etoplatezhi_payment = AsyncMock(
        return_value=types.SimpleNamespace(id=99),
    )

    def _fake_import(name: str):
        if name == 'app.services.payment_service':
            return fake_payment_service
        if name == 'app.database.crud.etoplatezhi':
            return fake_crud
        raise AssertionError(f'unexpected import_module({name!r})')

    monkeypatch.setattr(etp_payment, 'import_module', _fake_import)

    mixin = etp_payment.EtoplatezhiPaymentMixin()
    result = await mixin.create_etoplatezhi_payment(
        db=AsyncMock(),
        user_id=7,
        amount_kopeks=50000,
        payment_method_type=payment_method_type,
    )
    assert result is not None, 'payment creation should succeed'
    assert build_mock.call_count == 1
    return build_mock


@pytest.mark.asyncio
async def test_sbp_type_forces_sbp_qr(monkeypatch: pytest.MonkeyPatch) -> None:
    """payment_method_type='sbp' → build_payment_url(force_payment_method='sbp-qr')."""
    build_mock = await _call_create(monkeypatch, payment_method_type='sbp')
    assert build_mock.call_args.kwargs['force_payment_method'] == 'sbp-qr'


@pytest.mark.asyncio
async def test_card_type_forces_card_partner(monkeypatch: pytest.MonkeyPatch) -> None:
    """payment_method_type='card' → force_payment_method='card-partner'."""
    build_mock = await _call_create(monkeypatch, payment_method_type='card')
    assert build_mock.call_args.kwargs['force_payment_method'] == 'card-partner'


@pytest.mark.asyncio
async def test_unknown_type_forces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown payment_method_type → no force (None): full method picker."""
    build_mock = await _call_create(monkeypatch, payment_method_type='banana')
    assert build_mock.call_args.kwargs['force_payment_method'] is None


@pytest.mark.asyncio
async def test_none_type_forces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """payment_method_type=None → no force (None)."""
    build_mock = await _call_create(monkeypatch, payment_method_type=None)
    assert build_mock.call_args.kwargs['force_payment_method'] is None


# --------------------------------------------------------------------------- #
# (c) build_payment_url emits a signed force_payment_method URL param
# --------------------------------------------------------------------------- #
def test_build_payment_url_includes_signed_force_method() -> None:
    """The real build_payment_url must put force_payment_method=sbp-qr in the
    query string alongside a signature param (we don't assert the signature
    value, only that the param is present and the method is in the URL)."""
    service = EtoplatezhiService()
    url = service.build_payment_url(
        project_id=42,
        payment_id='order-1',
        payment_amount=50000,
        customer_id='123456',
        force_payment_method='sbp-qr',
    )

    query = parse_qs(urlparse(url).query)
    assert query.get('force_payment_method') == ['sbp-qr']
    assert 'signature' in query, 'URL must carry a signature param'
    assert query['signature'][0], 'signature must be non-empty'


def test_build_payment_url_omits_force_method_when_absent() -> None:
    """Without force_payment_method the param must not appear (default picker)."""
    service = EtoplatezhiService()
    url = service.build_payment_url(
        project_id=42,
        payment_id='order-2',
        payment_amount=50000,
        customer_id='123456',
    )

    query = parse_qs(urlparse(url).query)
    assert 'force_payment_method' not in query
    assert 'signature' in query
