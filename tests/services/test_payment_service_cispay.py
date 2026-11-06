"""CisPay orchestration contracts with strict synthetic provider records.

SQL locking/rollback/concurrent credit are covered by test_cispay_payments_postgres.py;
these tests retain create, validation, replay, terminal and HMAC scenarios without
mocking the removed CRUD-finalize split. A rejected payload leaves a retryable invoice,
instead of permanently poisoning it with the old amount_mismatch marker.
"""

import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.payment.cispay as core
from app.config import settings
from app.database.models import CisPayPayment, User
from app.services.cispay_service import CisPayService
from app.services.payment_service import PaymentService


class Session:
    """In-memory unit double; deliberately makes no claims about DB atomicity."""

    def __init__(self, payment=None):
        self.payment = payment
        self.user = SimpleNamespace(
            id=77,
            telegram_id=123456,
            status='active',
            restriction_topup=False,
            balance_kopeks=100,
            has_made_first_topup=False,
        )
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def get(self, model, _key, **_kwargs):
        return self.user if model is User else self.payment

    async def scalar(self, statement):
        return self.user if statement.column_descriptions[0]['entity'] is User else self.payment


def payment(**overrides):
    values = dict(
        id=7,
        user_id=77,
        order_id='cis_masked',
        cispay_payment_id=None,
        amount_kopeks=50000,
        currency='RUB',
        payment_method='CARD',
        status='pending',
        is_paid=False,
        is_sandbox=False,
        metadata_json={'store_id': 'masked-shop'},
        transaction_id=None,
        expires_at=None,
        next_check_at=None,
    )
    return CisPayPayment(**(values | overrides))


def payload(**overrides):
    return (
        dict(
            id='masked-invoice',
            store_id='masked-shop',
            order_id='cis_masked',
            payment_method='CARD',
            status='PAID',
            amount=50000,
            currency='RUB',
            charged_amount=51750,
            is_sandbox=False,
        )
        | overrides
    )


@pytest.fixture
def service(monkeypatch):
    instance = PaymentService.__new__(PaymentService)
    instance.bot = None
    instance._after_cispay_payment = AsyncMock()
    monkeypatch.setattr(core, 'create_transaction', AsyncMock(return_value=SimpleNamespace(id=9)))
    return instance


@pytest.fixture
def creation(monkeypatch):
    for key, value in dict(
        CISPAY_ENABLED=True,
        CISPAY_SHOP_ID='masked-shop',
        CISPAY_API_KEY='synthetic-key',
        CISPAY_CARD_ENABLED=True,
        CISPAY_SBP_ENABLED=True,
        CISPAY_CRYPTO_ENABLED=False,
    ).items():
        monkeypatch.setattr(settings, key, value)
    db = Session()
    config = SimpleNamespace(shop_id='masked-shop')

    async def policy(_db, _user, selected):
        method = core.resolve_cispay_method(selected)
        return {
            'selected_method': method,
            'min_amount_kopeks': 10000,
            'max_amount_kopeks': 10000000,
            'options': [{'id': 'card'}, {'id': 'sbp'}],
        }

    monkeypatch.setattr('app.services.payment_method_config_service.get_cispay_checkout_policy', policy)

    async def create_record(_db, **values):
        db.payment = payment(**values)
        await db.commit()
        return db.payment

    monkeypatch.setattr(core, 'create_record', create_record)

    async def create_remote(**kwargs):
        assert db.commit.await_count == 1  # invoice is durable before POST
        return {
            'id': 'masked-invoice',
            'order_id': kwargs['order_id'],
            'status': 'PENDING',
            'amount': kwargs['amount_kopeks'],
            'charged_amount': 51750,
            'payment_url': 'https://pay.example/invoice',
        }

    remote = SimpleNamespace(
        snapshot=lambda: config,
        get_store_capabilities=AsyncMock(
            return_value={
                'store_id': config.shop_id,
                'is_active': True,
                'payment_methods': [{'payment_method': m, 'is_active': True} for m in ('CARD', 'SBP')],
            }
        ),
        create_payment=AsyncMock(side_effect=create_remote),
    )
    monkeypatch.setattr(core, 'cispay_service', remote)
    return db, remote


async def test_create_cispay_payment_success(service, creation):
    db, remote = creation
    result = await service.create_cispay_payment(db, user_id=77, amount_kopeks=50000)
    assert result['local_payment_id'] == 7
    assert result['payment_url'] == 'https://pay.example/invoice'
    assert result['amount_kopeks'] == 50000
    assert result['order_id'].startswith('cis_')
    assert len(result['order_id']) == 36  # opaque UUID, no Telegram identity
    assert remote.create_payment.await_args.kwargs['payment_method'] == 'CARD'
    assert remote.create_payment.await_args.kwargs['customer_id'] == '123456'
    assert db.payment.charged_amount_kopeks == 51750
    assert result['expires_at'] is None  # create schema promises no expiry


async def test_create_cispay_payment_sbp_sub_method(service, creation):
    db, remote = creation
    result = await service.create_cispay_payment(db, user_id=77, amount_kopeks=50000, payment_method_type='sbp')
    assert result is not None
    assert remote.create_payment.await_args.kwargs['payment_method'] == 'SBP'


async def test_create_cispay_payment_respects_amount_limits(service, creation):
    db, remote = creation
    for amount in (9999, 10000001, True, 10000.1):
        assert await service.create_cispay_payment(db, user_id=77, amount_kopeks=amount) is None
    remote.create_payment.assert_not_awaited()


async def test_create_cispay_payment_disabled(service, monkeypatch):
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', False)
    assert await service.create_cispay_payment(Session(), user_id=77, amount_kopeks=50000) is None


async def test_process_cispay_callback_paid_finalizes(service):
    db = Session(payment())
    assert await service.process_cispay_callback(db, payload())
    assert db.payment.is_paid and db.payment.status == 'success'
    assert db.payment.cispay_payment_id == 'masked-invoice'
    assert db.user.balance_kopeks == 50100
    assert db.payment.transaction_id == 9
    service._after_cispay_payment.assert_awaited_once_with(db, 'topup', 77, 7, 100)


@pytest.mark.parametrize('amount', [49999, 'not-a-number', True, 50000.1, None])
async def test_process_cispay_callback_bad_amount_does_not_credit(service, amount):
    db = Session(payment())
    data = payload(amount=amount)
    if amount is None:
        del data['amount']
    with pytest.raises(core.CisPayValidationError):
        await service.process_cispay_callback(db, data)
    assert not db.payment.is_paid and db.payment.status == 'pending'
    assert db.user.balance_kopeks == 100
    service._after_cispay_payment.assert_not_awaited()


async def test_process_cispay_callback_already_paid_is_idempotent(service):
    db = Session(payment(status='success', is_paid=True, transaction_id=9))
    assert await service.process_cispay_callback(db, payload())
    assert db.user.balance_kopeks == 100
    core.create_transaction.assert_not_awaited()
    service._after_cispay_payment.assert_not_awaited()


async def test_process_cispay_callback_terminal_cannot_bypass_validation(service):
    # Old unconditional sticky early return hid mismatched identity/amount.
    # A later authoritative PAID may supersede declined; it must still validate.
    db = Session(payment(status='declined'))
    with pytest.raises(core.CisPayValidationError):
        await service.process_cispay_callback(db, payload(amount=49999))
    assert not db.payment.is_paid and db.payment.status == 'declined'
    service._after_cispay_payment.assert_not_awaited()


async def test_process_cispay_callback_missing_fields(service):
    with pytest.raises(core.CisPayValidationError):
        await service.process_cispay_callback(Session(), {'id': 'masked'})


async def test_process_cispay_callback_non_paid_status_updates(service):
    db = Session(payment())
    assert await service.process_cispay_callback(db, payload(status='EXPIRED'))
    assert db.payment.status == 'expired' and not db.payment.is_paid
    core.create_transaction.assert_not_awaited()
    service._after_cispay_payment.assert_not_awaited()


async def test_generic_method_falls_back_to_sbp_when_card_disabled(service, creation, monkeypatch):
    monkeypatch.setattr(settings, 'CISPAY_CARD_ENABLED', False)
    db, remote = creation
    assert await service.create_cispay_payment(db, user_id=77, amount_kopeks=50000)
    assert remote.create_payment.await_args.kwargs['payment_method'] == 'SBP'


def test_is_cispay_enabled_rejects_blank_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая строка ключа не должна включать шлюз — иначе HMAC вебхука подделывается."""
    monkeypatch.setattr(settings, 'CISPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CISPAY_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', '', raising=False)

    assert settings.is_cispay_enabled() is False

    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    assert settings.is_cispay_enabled() is True


def test_verify_webhook_signature_blank_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', '', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    forged = hmac.new(b'', raw_body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(raw_body, forged) is False


def test_verify_webhook_signature_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    signature = hmac.new(b'cis_sec_test', raw_body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(raw_body, signature) is True
    # Регистр hex-подписи не важен
    assert service.verify_webhook_signature(raw_body, signature.upper()) is True


def test_verify_webhook_signature_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    wrong = hmac.new(b'other_key', raw_body, hashlib.sha256).hexdigest()

    assert service.verify_webhook_signature(raw_body, wrong) is False
    assert service.verify_webhook_signature(raw_body, None) is False
    assert service.verify_webhook_signature(raw_body, '') is False


def test_verify_webhook_signature_tampered_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CISPAY_API_KEY', 'cis_sec_test', raising=False)
    service = CisPayService()

    raw_body = b'{"order_id":"cis1_x","status":"PAID","amount":50000}'
    signature = hmac.new(b'cis_sec_test', raw_body, hashlib.sha256).hexdigest()
    tampered = raw_body.replace(b'50000', b'99999')

    assert service.verify_webhook_signature(tampered, signature) is False


@pytest.mark.parametrize('previous_status', ['declined', 'expired'])
async def test_valid_late_payment_can_settle_unpaid_invoice(service, previous_status):
    db = Session(payment(status=previous_status))
    assert await service.process_cispay_callback(db, payload())
    assert db.payment.is_paid and db.user.balance_kopeks == 50100
    core.create_transaction.assert_awaited_once()


async def test_unpaid_refunded_invoice_cannot_be_credited_by_late_paid(service):
    db = Session(payment(status='refunded'))
    assert await service.process_cispay_callback(db, payload())
    assert not db.payment.is_paid and db.payment.status == 'refunded'
    assert db.user.balance_kopeks == 100
    core.create_transaction.assert_not_awaited()


async def test_capabilities_disable_creation_before_post(service, creation):
    db, remote = creation
    remote.get_store_capabilities.return_value['is_active'] = False
    assert await service.create_cispay_payment(db, user_id=77, amount_kopeks=50000) is None
    remote.create_payment.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_generic_method_uses_active_sbp_capability(service, creation):
    db, remote = creation
    remote.get_store_capabilities.return_value['payment_methods'] = [{'payment_method': 'SBP', 'is_active': True}]
    assert await service.create_cispay_payment(db, user_id=77, amount_kopeks=50000)
    assert remote.create_payment.await_args.kwargs['payment_method'] == 'SBP'


async def test_email_only_customer_uses_stable_database_identity(service, creation):
    db, remote = creation
    db.user.telegram_id = None
    assert await service.create_cispay_payment(db, user_id=77, amount_kopeks=50000)
    assert remote.create_payment.await_args.kwargs['customer_id'] == 'customer-77'


async def test_generic_sbp_fallback_uses_sbp_minimum(service, creation, monkeypatch):
    db, remote = creation

    async def policy(_db, _user, selected):
        return {
            'selected_method': 'SBP' if selected == 'sbp' else 'CARD',
            'min_amount_kopeks': 1000 if selected == 'sbp' else 5000,
            'max_amount_kopeks': 10000000,
            'options': [{'id': 'card'}, {'id': 'sbp'}],
        }

    monkeypatch.setattr('app.services.payment_method_config_service.get_cispay_checkout_policy', policy)
    remote.get_store_capabilities.return_value['payment_methods'] = [{'payment_method': 'SBP', 'is_active': True}]
    assert await service.create_cispay_payment(db, user_id=77, amount_kopeks=1000)
    assert remote.create_payment.await_args.kwargs['payment_method'] == 'SBP'
