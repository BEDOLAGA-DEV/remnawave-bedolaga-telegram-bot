from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import payment_method_config_service as config_service


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'masked-shop')
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'example-secret')
    monkeypatch.setattr(settings, 'CISPAY_CARD_ENABLED', False)
    monkeypatch.setattr(settings, 'CISPAY_SBP_ENABLED', False)


def test_crypto_is_optin_and_not_in_default_options(configured):
    assert settings.CISPAY_CRYPTO_ENABLED is False
    assert {option['id'] for option in config_service._get_method_defaults()['cispay']['available_sub_options']} == {
        'card',
        'sbp',
    }


def test_explicit_methods_limit_options(configured, monkeypatch):
    monkeypatch.setattr(settings, 'CISPAY_SBP_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', True)
    assert {option['id'] for option in config_service._get_method_defaults()['cispay']['available_sub_options']} == {
        'sbp',
        'crypto',
    }


async def test_effective_policy_respects_database_options_and_limits(configured, monkeypatch):
    monkeypatch.setattr(
        config_service,
        'get_config_by_method_id',
        AsyncMock(
            return_value=SimpleNamespace(
                user_type_filter='all', first_topup_filter='any', promo_group_filter_mode='all'
            )
        ),
    )
    monkeypatch.setattr(
        config_service,
        'get_enabled_methods_for_user',
        AsyncMock(
            return_value=[
                {
                    'id': 'cispay',
                    'min_amount_kopeks': 1000,
                    'max_amount_kopeks': 20000,
                    'options': [{'id': 'sbp', 'name': 'СБП'}],
                }
            ]
        ),
    )
    policy = await config_service.get_cispay_checkout_policy(AsyncMock(), None)
    assert policy['min_amount_kopeks'] == 1000
    assert [option['id'] for option in policy['options']] == ['sbp']


async def test_default_is_explicit_fiat_and_card_minimum(configured, monkeypatch):
    monkeypatch.setattr(config_service, 'get_config_by_method_id', AsyncMock(return_value=None))
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', True)
    monkeypatch.setattr(settings, 'CISPAY_MIN_AMOUNT_KOPEKS', 100)
    policy = await config_service.get_cispay_checkout_policy(AsyncMock(), None)
    assert policy['selected_method'] == 'CARD'
    assert policy['min_amount_kopeks'] == 5000
    crypto = await config_service.get_cispay_checkout_policy(AsyncMock(), None, 'crypto')
    assert crypto['selected_method'] == 'CRYPTO'
    assert crypto['min_amount_kopeks'] == 100


async def test_guest_metadata_exists_before_create(configured, monkeypatch):
    from app.services.payment_service import PaymentService

    service = PaymentService()
    service.create_cispay_payment = AsyncMock(
        return_value={
            'payment_url': 'https://invalid.example/pay',
            'order_id': 'masked-order',
            'local_payment_id': 1,
        }
    )
    db = AsyncMock()
    result = await service.create_guest_payment(
        db,
        amount_kopeks=10000,
        payment_method='cispay_sbp',
        description='Example',
        purchase_token='masked-token',
        return_url='https://invalid.example/result',
    )
    assert result['payment_id'] == 'masked-order'
    assert service.create_cispay_payment.await_args.kwargs['metadata']['purchase_token'] == 'masked-token'
    db.commit.assert_not_awaited()


async def test_telegram_accepts_effective_sbp_minimum(configured, monkeypatch):
    from app.handlers.balance import cispay

    monkeypatch.setattr(
        cispay,
        'get_cispay_checkout_policy',
        AsyncMock(
            return_value={
                'selected_method': 'SBP',
                'min_amount_kopeks': 1000,
                'max_amount_kopeks': 20000,
            }
        ),
    )
    create = AsyncMock()
    monkeypatch.setattr(cispay, '_create_cispay_payment_and_respond', create)
    state = AsyncMock()
    state.get_data.return_value = {'payment_method': 'cispay_sbp'}
    await cispay.process_cispay_payment_amount(
        AsyncMock(), SimpleNamespace(id=1, language='ru', restriction_topup=False), AsyncMock(), 1000, state
    )
    assert create.await_args.kwargs['amount_kopeks'] == 1000
    assert create.await_args.kwargs['payment_method_type'] == 'sbp'


async def test_crypto_disabled_rejected_even_if_requested(configured, monkeypatch):
    monkeypatch.setattr(config_service, 'get_config_by_method_id', AsyncMock(return_value=None))
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', False)
    assert await config_service.get_cispay_checkout_policy(AsyncMock(), None, 'crypto') is None


async def test_guest_recovery_excludes_sandbox():
    from unittest.mock import Mock

    from sqlalchemy.dialects import postgresql

    from app.services.guest_purchase_service import _find_succeeded_provider_payment

    result = Mock()
    result.scalars.return_value.first.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    assert await _find_succeeded_provider_payment(db, 'cispay', 'masked-token') is None
    sql = str(
        db.execute.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
    )
    assert 'cispay_payments.is_sandbox IS false' in sql
    assert 'cispay_payments.is_paid IS true' in sql


def test_sandbox_does_not_appear_as_credited_in_cabinet():
    from datetime import UTC, datetime

    from app.cabinet.routes.balance import _record_to_response
    from app.database.models import PaymentMethod
    from app.services.payment_verification_service import PendingPayment

    payment = SimpleNamespace(is_sandbox=True, payment_url=None)
    record = PendingPayment(
        method=PaymentMethod.CISPAY,
        local_id=1,
        identifier='masked-order',
        amount_kopeks=10000,
        status='success',
        is_paid=True,
        created_at=datetime.now(UTC),
        user=None,
        payment=payment,
    )
    result = _record_to_response(record)
    assert not result.is_paid
    assert result.status == 'test_paid'
    assert not result.is_checkable


def test_non_ruble_config_cannot_create_new_payments(configured, monkeypatch):
    monkeypatch.setattr(settings, 'CISPAY_CURRENCY', 'USD')
    assert settings.is_cispay_configured()
    assert not settings.is_cispay_enabled()


async def test_guest_cannot_bypass_database_group_restriction(configured, monkeypatch):
    monkeypatch.setattr(
        config_service,
        'get_config_by_method_id',
        AsyncMock(
            return_value=SimpleNamespace(
                user_type_filter='all',
                first_topup_filter='any',
                promo_group_filter_mode='selected',
            )
        ),
    )
    assert await config_service.get_cispay_checkout_policy(AsyncMock(), None, 'sbp') is None


async def test_crypto_only_configuration_requires_explicit_selection(configured, monkeypatch):
    monkeypatch.setattr(settings, 'CISPAY_CRYPTO_ENABLED', True)
    monkeypatch.setattr(
        config_service,
        'get_config_by_method_id',
        AsyncMock(
            return_value=SimpleNamespace(
                user_type_filter='all',
                first_topup_filter='any',
                promo_group_filter_mode='all',
            )
        ),
    )
    monkeypatch.setattr(
        config_service,
        'get_enabled_methods_for_user',
        AsyncMock(
            return_value=[
                {
                    'id': 'cispay',
                    'min_amount_kopeks': 1000,
                    'max_amount_kopeks': 20000,
                    'options': [{'id': 'crypto', 'name': 'Crypto'}],
                }
            ]
        ),
    )
    assert (await config_service.get_cispay_checkout_policy(AsyncMock(), None))['selected_method'] is None
    assert (await config_service.get_cispay_checkout_policy(AsyncMock(), None, 'crypto'))['selected_method'] == 'CRYPTO'
