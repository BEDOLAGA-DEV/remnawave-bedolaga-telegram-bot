import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

BACKUP_DIR = ROOT_DIR / 'data' / 'backups'
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

os.environ.setdefault('BOT_TOKEN', 'test-token')

from app.config import settings
from app.database.models import PaymentMethod
from app.services.payment.cryptobot import CryptoBotPaymentMixin
from app.services.pricing_engine import PricingEngine
from app.services.subscription_renewal_service import (
    SubscriptionRenewalPricing,
    SubscriptionRenewalResult,
    build_payment_descriptor,
    decode_payment_payload,
    encode_payment_payload,
)
from app.webapi.routes import miniapp
from app.webapi.schemas.miniapp import (
    MiniAppPaymentCreateRequest,
    MiniAppPaymentIntegrationType,
    MiniAppPaymentMethodsRequest,
    MiniAppPaymentStatusQuery,
    MiniAppSubscriptionRenewalRequest,
    MiniAppTariffPurchaseRequest,
)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_compute_cryptobot_limits_scale_with_rate():
    low_rate_min, low_rate_max = miniapp._compute_cryptobot_limits(70.0)
    high_rate_min, high_rate_max = miniapp._compute_cryptobot_limits(120.0)

    assert low_rate_min == 7000
    assert low_rate_max == 7000000
    assert high_rate_min == 12000
    assert high_rate_max == 12000000
    assert high_rate_min > low_rate_min
    assert high_rate_max > low_rate_max


def test_encode_decode_renewal_payload_preserves_snapshot():
    pricing_model = SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=12000,
        discounted_total=10000,
        final_total=9000,
        promo_discount_value=1000,
        promo_discount_percent=10,
        overall_discount_percent=25,
        per_month=9000,
        server_ids=[1, 2],
        details={'servers_price_per_month': 1000},
    )

    descriptor = build_payment_descriptor(
        user_id=1,
        subscription_id=42,
        period_days=30,
        total_amount_kopeks=pricing_model.final_total,
        missing_amount_kopeks=1000,
        pricing_snapshot=pricing_model.to_payload(),
    )

    payload_value = encode_payment_payload(descriptor)
    decoded = decode_payment_payload(payload_value, expected_user_id=1)

    assert decoded is not None
    assert decoded.total_amount_kopeks == 9000
    assert decoded.missing_amount_kopeks == 1000
    assert decoded.pricing_snapshot is not None
    assert decoded.pricing_snapshot.get('server_ids') == [1, 2]


@pytest.mark.anyio('asyncio')
async def test_submit_subscription_renewal_uses_balance_when_sufficient(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'BOT_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False, raising=False)
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'ru', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', None, raising=False)
    monkeypatch.setattr(type(settings), 'get_available_renewal_periods', lambda self: [30], raising=False)

    user = types.SimpleNamespace(id=10, balance_kopeks=10000, language='ru')
    subscription = types.SimpleNamespace(
        id=77,
        connected_squads=[],
        traffic_limit_gb=100,
        device_limit=5,
        end_date=datetime.now(UTC),
    )

    pricing_model = SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=10000,
        discounted_total=10000,
        final_total=10000,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=10000,
        server_ids=[],
        details={},
    )

    async def fake_authorize(init_data, db):
        return user

    def fake_ensure(subscription_user, allowed_statuses=None):
        return subscription

    async def fake_calculate(db, u, sub, period):
        return pricing_model

    captured: dict[str, Any] = {}

    async def fake_finalize(db, u, sub, pricing, *, charge_balance_amount=None, description=None, payment_method=None):
        charge = charge_balance_amount if charge_balance_amount is not None else pricing.final_total
        captured['charge'] = charge
        captured['description'] = description
        return SubscriptionRenewalResult(
            subscription=types.SimpleNamespace(id=sub.id, end_date=datetime.now(UTC)),
            transaction=types.SimpleNamespace(id=501),
            total_amount_kopeks=pricing.final_total,
            charged_from_balance_kopeks=charge,
            old_end_date=sub.end_date,
        )

    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', fake_authorize)
    monkeypatch.setattr(miniapp, '_ensure_paid_subscription', fake_ensure)
    monkeypatch.setattr(miniapp, '_validate_subscription_id', lambda *args, **kwargs: None)
    monkeypatch.setattr(miniapp, '_calculate_subscription_renewal_pricing', fake_calculate)
    monkeypatch.setattr(miniapp.renewal_service, 'finalize', fake_finalize)

    payload = MiniAppSubscriptionRenewalRequest(
        initData='init',
        subscriptionId=77,
        periodId='days:30',
    )

    response = await miniapp.submit_subscription_renewal_endpoint(payload, db=types.SimpleNamespace())

    assert response.success is True
    assert response.requires_payment is False
    assert response.subscription_id == 77
    assert response.renewed_until is not None
    assert 'Подписка' in (response.message or '')
    assert captured['charge'] == 10000


@pytest.mark.anyio('asyncio')
async def test_submit_subscription_renewal_returns_cryptobot_invoice(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'BOT_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False, raising=False)
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'ru', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_DEFAULT_ASSET', 'USDT', raising=False)
    monkeypatch.setattr(type(settings), 'get_available_renewal_periods', lambda self: [30], raising=False)

    user = types.SimpleNamespace(id=15, balance_kopeks=5000, language='ru')
    subscription = types.SimpleNamespace(
        id=88,
        connected_squads=[],
        traffic_limit_gb=100,
        device_limit=5,
        end_date=datetime.now(UTC),
    )

    pricing_model = SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=20000,
        discounted_total=20000,
        final_total=20000,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=20000,
        server_ids=[],
        details={},
    )

    async def fake_authorize(init_data, db):
        return user

    def fake_ensure(subscription_user, allowed_statuses=None):
        return subscription

    async def fake_calculate(db, u, sub, period):
        return pricing_model

    created_calls: dict[str, Any] = {}

    class DummyPaymentService:
        def __init__(self, *args, **kwargs):
            pass

        async def create_cryptobot_payment(self, db, **kwargs):
            created_calls.update(kwargs)
            return {
                'local_payment_id': 321,
                'invoice_id': 'inv_123',
                'bot_invoice_url': 'https://t.me/invoice',
                'mini_app_invoice_url': 'https://mini.app/pay',
                'web_app_invoice_url': None,
            }

    async def fake_rate():
        return 100.0

    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', fake_authorize)
    monkeypatch.setattr(miniapp, '_ensure_paid_subscription', fake_ensure)
    monkeypatch.setattr(miniapp, '_validate_subscription_id', lambda *args, **kwargs: None)
    monkeypatch.setattr(miniapp, '_calculate_subscription_renewal_pricing', fake_calculate)
    monkeypatch.setattr(miniapp, 'PaymentService', lambda *args, **kwargs: DummyPaymentService())
    monkeypatch.setattr(miniapp, '_get_usd_to_rub_rate', fake_rate)

    payload = MiniAppSubscriptionRenewalRequest(
        initData='init',
        subscriptionId=88,
        periodId='days:30',
        method='cryptobot',
    )

    response = await miniapp.submit_subscription_renewal_endpoint(payload, db=types.SimpleNamespace())

    assert response.success is False
    assert response.requires_payment is True
    assert response.payment_method == 'cryptobot'
    assert response.payment_amount_kopeks == 15000
    assert response.payment_url == 'https://mini.app/pay'
    assert response.invoice_id == 'inv_123'
    assert response.payment_id == 321
    assert response.payment_payload and response.payment_payload.startswith('subscription_renewal')
    assert created_calls.get('amount_usd') == pytest.approx(1.5)
    assert created_calls.get('description') == 'Продление подписки на 30 дней'


@pytest.mark.anyio('asyncio')
async def test_purchase_tariff_creates_new_subscription_for_same_tariff_in_multi_mode(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True, raising=False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True, raising=False)

    existing_sub = types.SimpleNamespace(id=41, tariff_id=7, is_active=True, device_limit=9)
    user = types.SimpleNamespace(
        id=10,
        telegram_id=10,
        balance_kopeks=50000,
        language='ru',
        subscriptions=[existing_sub],
        subscription=existing_sub,
        restriction_subscription=False,
    )
    tariff = types.SimpleNamespace(
        id=7,
        name='Pro',
        is_active=True,
        is_daily=False,
        traffic_limit_gb=100,
        device_limit=3,
        allowed_squads=['squad-1'],
        is_available_for_promo_group=lambda promo_group_id: True,
    )
    pricing_result = types.SimpleNamespace(
        final_total=10000,
        promo_offer_discount=0,
        breakdown={'group_discount_pct': {'period': 0}},
    )

    async def fake_authorize(init_data, db):
        return user

    async def fake_lock_user(db, user_id):
        return user

    created: dict[str, Any] = {}

    async def fake_create_paid_subscription(**kwargs):
        created.update(kwargs)
        return types.SimpleNamespace(id=77, end_date=datetime.now(UTC) + timedelta(days=30))

    async def fail_extend(*args, **kwargs):
        raise AssertionError('extend_subscription should not be used for same-tariff purchase in multi mode')

    class DummySubscriptionService:
        async def update_remnawave_user(self, db, subscription, **kwargs):
            return None

    class DummyDB:
        async def refresh(self, obj):
            return None

    async def fake_get_tariff(db, tariff_id):
        return tariff

    async def fake_calculate(*args, **kwargs):
        return pricing_result

    async def fake_subtract(*args, **kwargs):
        return True

    async def fake_create_transaction(*args, **kwargs):
        return None

    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', fake_authorize)
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', fake_lock_user)
    monkeypatch.setattr(miniapp, 'get_tariff_by_id', fake_get_tariff)
    monkeypatch.setattr(miniapp.pricing_engine, 'calculate_tariff_purchase_price', fake_calculate)
    monkeypatch.setattr(miniapp, 'subtract_user_balance', fake_subtract)
    monkeypatch.setattr(miniapp, 'create_transaction', fake_create_transaction)
    monkeypatch.setattr('app.database.crud.subscription.create_paid_subscription', fake_create_paid_subscription)
    monkeypatch.setattr(miniapp, 'extend_subscription', fail_extend)
    monkeypatch.setattr(miniapp, 'SubscriptionService', lambda: DummySubscriptionService())

    payload = MiniAppTariffPurchaseRequest(initData='init', tariffId=7, periodDays=30)
    response = await miniapp.purchase_tariff_endpoint(payload, db=DummyDB())

    assert response.success is True
    assert response.subscription_id == 77
    assert created['user_id'] == user.id
    assert created['tariff_id'] == tariff.id
    assert created['device_limit'] == tariff.device_limit


@pytest.mark.anyio('asyncio')
async def test_submit_subscription_renewal_rounds_up_cryptobot_amount(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'BOT_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False, raising=False)
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'ru', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_API_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_DEFAULT_ASSET', 'USDT', raising=False)
    monkeypatch.setattr(type(settings), 'get_available_renewal_periods', lambda self: [30], raising=False)

    user = types.SimpleNamespace(id=42, balance_kopeks=0, language='ru')
    subscription = types.SimpleNamespace(
        id=99,
        connected_squads=[],
        traffic_limit_gb=100,
        device_limit=5,
        end_date=datetime.now(UTC),
    )

    pricing_model = SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=9512,
        discounted_total=9512,
        final_total=9512,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=9512,
        server_ids=[],
        details={},
    )

    async def fake_authorize(init_data, db):
        return user

    def fake_ensure(subscription_user, allowed_statuses=None):
        return subscription

    async def fake_calculate(db, u, sub, period):
        return pricing_model

    captured: dict[str, Any] = {}

    class DummyPaymentService:
        def __init__(self, *args, **kwargs):
            pass

        async def create_cryptobot_payment(self, db, **kwargs):
            captured.update(kwargs)
            return {
                'local_payment_id': 654,
                'invoice_id': 'inv_round',
                'bot_invoice_url': 'https://t.me/pay',
                'mini_app_invoice_url': 'https://mini.app/pay-round',
                'web_app_invoice_url': None,
            }

    async def fake_rate():
        return 95.0

    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', fake_authorize)
    monkeypatch.setattr(miniapp, '_ensure_paid_subscription', fake_ensure)
    monkeypatch.setattr(miniapp, '_validate_subscription_id', lambda *args, **kwargs: None)
    monkeypatch.setattr(miniapp, '_calculate_subscription_renewal_pricing', fake_calculate)
    monkeypatch.setattr(miniapp, 'PaymentService', lambda *args, **kwargs: DummyPaymentService())
    monkeypatch.setattr(miniapp, '_get_usd_to_rub_rate', fake_rate)

    payload = MiniAppSubscriptionRenewalRequest(
        initData='init',
        subscriptionId=99,
        periodId='days:30',
        method='cryptobot',
    )

    response = await miniapp.submit_subscription_renewal_endpoint(payload, db=types.SimpleNamespace())

    assert response.requires_payment is True
    assert captured.get('amount_usd') == pytest.approx(1.01)
    assert response.payment_amount_kopeks == 9512


@pytest.mark.anyio('asyncio')
async def test_cryptobot_renewal_uses_pricing_snapshot(monkeypatch):
    module = sys.modules['app.services.payment.cryptobot']
    mixin = CryptoBotPaymentMixin()

    subscription = types.SimpleNamespace(
        id=77, connected_squads=[], traffic_limit_gb=100, device_limit=5, tariff=None, tariff_id=None
    )
    user = types.SimpleNamespace(id=5, balance_kopeks=7000, subscription=subscription)

    pricing_model = SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=12000,
        discounted_total=10000,
        final_total=10000,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=10000,
        server_ids=[11, 22],
        details={'servers_individual_prices': [500, 500]},
    )

    descriptor = build_payment_descriptor(
        user_id=5,
        subscription_id=77,
        period_days=30,
        total_amount_kopeks=10000,
        missing_amount_kopeks=3000,
        pricing_snapshot=pricing_model.to_payload(),
    )

    payment = types.SimpleNamespace(invoice_id='INV-1', user_id=5)

    async def fake_get_user_by_id(db, user_id):
        return user if user_id == 5 else None

    monkeypatch.setitem(
        sys.modules, 'app.services.payment_service', types.SimpleNamespace(get_user_by_id=fake_get_user_by_id)
    )

    async def fail_calculate(*args, **kwargs):
        raise AssertionError('PricingEngine.calculate_renewal_price should not be called when snapshot is present')

    monkeypatch.setattr(PricingEngine, 'calculate_renewal_price', fail_calculate)

    captured: dict[str, Any] = {}

    async def fake_finalize(db, u, sub, pricing, *, charge_balance_amount=None, description=None, payment_method=None):
        captured['pricing'] = pricing
        captured['charge'] = charge_balance_amount
        captured['description'] = description
        captured['payment_method'] = payment_method
        return SubscriptionRenewalResult(
            subscription=types.SimpleNamespace(id=sub.id, end_date=datetime.now(UTC)),
            transaction=types.SimpleNamespace(id=999),
            total_amount_kopeks=pricing.final_total,
            charged_from_balance_kopeks=charge_balance_amount or pricing.final_total,
            old_end_date=None,
        )

    monkeypatch.setattr(module.renewal_service, 'finalize', fake_finalize)

    async def fake_link(db, invoice_id, transaction_id):
        captured['linked'] = (invoice_id, transaction_id)

    cryptobot_crud = types.SimpleNamespace(link_cryptobot_payment_to_transaction=fake_link)

    result = await mixin._process_subscription_renewal_payment(
        db=types.SimpleNamespace(),
        payment=payment,
        descriptor=descriptor,
        cryptobot_crud=cryptobot_crud,
    )

    assert result is True
    assert captured['pricing'].server_ids == [11, 22]
    assert captured['pricing'].final_total == 10000
    assert captured['charge'] == 7000
    assert captured['payment_method'] == PaymentMethod.CRYPTOBOT
    assert captured['linked'] == ('INV-1', 999)


@pytest.mark.anyio('asyncio')
async def test_cryptobot_renewal_accepts_changed_pricing_without_snapshot(monkeypatch):
    module = sys.modules['app.services.payment.cryptobot']
    mixin = CryptoBotPaymentMixin()

    subscription = types.SimpleNamespace(
        id=55, connected_squads=[], traffic_limit_gb=50, device_limit=3, tariff=None, tariff_id=None
    )
    user = types.SimpleNamespace(id=8, balance_kopeks=4000, subscription=subscription)

    descriptor = build_payment_descriptor(
        user_id=8,
        subscription_id=55,
        period_days=30,
        total_amount_kopeks=5000,
        missing_amount_kopeks=1000,
    )

    payment = types.SimpleNamespace(invoice_id='INV-2', user_id=8)

    async def fake_get_user_by_id(db, user_id):
        return user if user_id == 8 else None

    monkeypatch.setitem(
        sys.modules, 'app.services.payment_service', types.SimpleNamespace(get_user_by_id=fake_get_user_by_id)
    )

    # Recalculated price is LOWER than descriptor — user benefits, should proceed
    recalculated_pricing = SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=4800,
        discounted_total=4800,
        final_total=4800,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=4800,
        server_ids=[],
        details={},
    )

    async def fake_calculate(self, db, sub, period_days, *, user=None):
        return recalculated_pricing

    monkeypatch.setattr(PricingEngine, 'calculate_renewal_price', fake_calculate)

    captured: dict[str, Any] = {}

    async def fake_finalize(db, u, sub, pricing, *, charge_balance_amount=None, description=None, payment_method=None):
        captured['pricing'] = pricing
        captured['charge'] = charge_balance_amount
        return SubscriptionRenewalResult(
            subscription=types.SimpleNamespace(id=sub.id, end_date=datetime.now(UTC)),
            transaction=None,
            total_amount_kopeks=pricing.final_total,
            charged_from_balance_kopeks=charge_balance_amount or pricing.final_total,
            old_end_date=None,
        )

    monkeypatch.setattr(module.renewal_service, 'finalize', fake_finalize)

    async def noop_link(*args, **kwargs):
        return None

    cryptobot_crud = types.SimpleNamespace(link_cryptobot_payment_to_transaction=noop_link)

    result = await mixin._process_subscription_renewal_payment(
        db=types.SimpleNamespace(),
        payment=payment,
        descriptor=descriptor,
        cryptobot_crud=cryptobot_crud,
    )

    assert result is True
    # With C-4 fix: recalculated price (4800) < descriptor (5000) → use recalculated
    assert captured['pricing'].final_total == 4800
    # M-5 fix: required_balance = max(0, final_total - missing) = max(0, 4800 - 1000) = 3800
    assert captured['charge'] == 3800


@pytest.mark.anyio('asyncio')
async def test_cryptobot_webhook_uses_inline_payload_when_db_missing(monkeypatch):
    module = sys.modules['app.services.payment.cryptobot']
    mixin = CryptoBotPaymentMixin()

    subscription = types.SimpleNamespace(
        id=91, connected_squads=[], traffic_limit_gb=80, device_limit=4, tariff=None, tariff_id=None
    )
    user = types.SimpleNamespace(id=21, balance_kopeks=6000, subscription=subscription)

    pricing_model = SubscriptionRenewalPricing(
        period_days=30,
        period_id='days:30',
        months=1,
        base_original_total=9000,
        discounted_total=9000,
        final_total=9000,
        promo_discount_value=0,
        promo_discount_percent=0,
        overall_discount_percent=0,
        per_month=9000,
        server_ids=[5, 6],
        details={'servers_individual_prices': [300, 300]},
    )

    descriptor = build_payment_descriptor(
        user_id=21,
        subscription_id=91,
        period_days=30,
        total_amount_kopeks=9000,
        missing_amount_kopeks=3000,
        pricing_snapshot=pricing_model.to_payload(),
    )

    encoded_payload = encode_payment_payload(descriptor)

    payment = types.SimpleNamespace(
        invoice_id='INV-webhook',
        user_id=21,
        status='active',
        payload=None,
        amount='90.00',
        asset='USDT',
        bot_invoice_url=None,
        mini_app_invoice_url=None,
        web_app_invoice_url=None,
        description='Продление подписки',
    )

    async def fake_get_user_by_id(db, user_id):
        return user if user_id == 21 else None

    monkeypatch.setitem(
        sys.modules,
        'app.services.payment_service',
        types.SimpleNamespace(get_user_by_id=fake_get_user_by_id),
    )

    async def fail_calculate(*args, **kwargs):
        raise AssertionError('PricingEngine.calculate_renewal_price should not be called')

    monkeypatch.setattr(PricingEngine, 'calculate_renewal_price', fail_calculate)

    captured: dict[str, Any] = {}

    async def fake_finalize(db, u, sub, pricing, *, charge_balance_amount=None, description=None, payment_method=None):
        captured['pricing'] = pricing
        captured['charge'] = charge_balance_amount
        captured['description'] = description
        captured['payment_method'] = payment_method
        return SubscriptionRenewalResult(
            subscription=types.SimpleNamespace(id=sub.id, end_date=datetime.now(UTC)),
            transaction=types.SimpleNamespace(id=1234),
            total_amount_kopeks=pricing.final_total,
            charged_from_balance_kopeks=charge_balance_amount or pricing.final_total,
            old_end_date=None,
        )

    monkeypatch.setattr(module.renewal_service, 'finalize', fake_finalize)

    linked: dict[str, Any] = {}

    async def fake_get(db, invoice_id):
        return payment if invoice_id == payment.invoice_id else None

    async def fake_update(db, invoice_id, status, paid_at):
        if invoice_id == payment.invoice_id:
            payment.status = status
            payment.paid_at = paid_at
        return payment

    async def fake_link(db, invoice_id, transaction_id):
        linked['value'] = (invoice_id, transaction_id)
        return payment

    monkeypatch.setitem(
        sys.modules,
        'app.database.crud.cryptobot',
        types.SimpleNamespace(
            get_cryptobot_payment_by_invoice_id=fake_get,
            update_cryptobot_payment_status=fake_update,
            link_cryptobot_payment_to_transaction=fake_link,
        ),
    )

    webhook_payload = {
        'update_type': 'invoice_paid',
        'payload': {
            'invoice_id': payment.invoice_id,
            'paid_at': '2024-05-01T12:00:00Z',
            'payload': encoded_payload,
        },
    }

    result = await mixin.process_cryptobot_webhook(types.SimpleNamespace(), webhook_payload)

    assert result is True
    assert captured['pricing'].final_total == 9000
    assert captured['charge'] == 6000
    assert captured['payment_method'] == PaymentMethod.CRYPTOBOT
    assert linked['value'] == (payment.invoice_id, 1234)


@pytest.mark.anyio('asyncio')
async def test_create_payment_link_pal24_uses_selected_option(monkeypatch):
    monkeypatch.setattr(settings, 'PAL24_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PAL24_API_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'PAL24_SHOP_ID', 'shop', raising=False)
    monkeypatch.setattr(settings, 'PAL24_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'PAL24_MAX_AMOUNT_KOPEKS', 5000000, raising=False)

    captured_calls = []

    class DummyPaymentService:
        def __init__(self, *args, **kwargs):
            pass

        async def create_pal24_payment(self, db, **kwargs):
            captured_calls.append({'db': db, **kwargs})
            return {
                'local_payment_id': 101,
                'bill_id': 'BILL42',
                'order_id': 'ORD42',
                'payment_method': kwargs.get('payment_method'),
                'sbp_url': 'https://sbp',
                'card_url': 'https://card',
                'link_url': 'https://link',
            }

    async def fake_resolve_user(db, init_data):
        return types.SimpleNamespace(id=123, language='ru'), {}

    monkeypatch.setattr(miniapp, 'PaymentService', lambda *args, **kwargs: DummyPaymentService())
    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', fake_resolve_user)

    payload = MiniAppPaymentCreateRequest(
        initData='test',
        method='pal24',
        amountKopeks=15000,
        option='card',
    )

    response = await miniapp.create_payment_link(payload, db=types.SimpleNamespace())

    assert response.payment_url == 'https://card'
    assert response.extra['selected_option'] == 'card'
    assert response.extra['payment_method'] == 'card'
    assert captured_calls and captured_calls[0]['payment_method'] == 'card'


@pytest.mark.anyio('asyncio')
async def test_create_payment_link_wata_returns_payload(monkeypatch):
    monkeypatch.setattr(settings, 'WATA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'WATA_ACCESS_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'WATA_TERMINAL_PUBLIC_ID', 'terminal', raising=False)
    monkeypatch.setattr(settings, 'WATA_MIN_AMOUNT_KOPEKS', 1000, raising=False)
    monkeypatch.setattr(settings, 'WATA_MAX_AMOUNT_KOPEKS', 5000000, raising=False)

    captured_call: dict[str, Any] = {}

    class DummyPaymentService:
        def __init__(self, *args, **kwargs):
            pass

        async def create_wata_payment(self, db, **kwargs):
            captured_call.update({'db': db, **kwargs})
            return {
                'local_payment_id': 202,
                'payment_link_id': 'link_202',
                'payment_url': 'https://wata.example/pay',
                'status': 'Opened',
                'order_id': 'order_202',
            }

    async def fake_resolve_user(db, init_data):
        return types.SimpleNamespace(id=555, language='ru'), {}

    monkeypatch.setattr(miniapp, 'PaymentService', lambda *args, **kwargs: DummyPaymentService())
    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', fake_resolve_user)

    payload = MiniAppPaymentCreateRequest(
        initData='init',
        method='wata',
        amountKopeks=25000,
    )

    response = await miniapp.create_payment_link(payload, db=types.SimpleNamespace())

    assert response.payment_url == 'https://wata.example/pay'
    assert response.amount_kopeks == 25000
    assert response.extra['local_payment_id'] == 202
    assert response.extra['payment_link_id'] == 'link_202'
    assert response.extra['payment_id'] == 'link_202'
    assert response.extra['order_id'] == 'order_202'
    assert 'requested_at' in response.extra

    assert captured_call.get('user_id') == 555
    assert captured_call.get('amount_kopeks') == 25000
    assert captured_call.get('description')


@pytest.mark.anyio('asyncio')
async def test_resolve_yookassa_status_includes_identifiers(monkeypatch):
    payment = types.SimpleNamespace(
        id=55,
        user_id=1,
        amount_kopeks=15000,
        currency='RUB',
        status='pending',
        is_paid=False,
        captured_at=None,
        updated_at=None,
        created_at=datetime.now(UTC),
        transaction_id=42,
        yookassa_payment_id='yk_1',
    )

    async def fake_get_by_local_id(db, local_id):
        return payment if local_id == 55 else None

    async def fake_get_by_id(db, payment_id):
        return None

    stub_module = types.SimpleNamespace(
        get_yookassa_payment_by_local_id=fake_get_by_local_id,
        get_yookassa_payment_by_id=fake_get_by_id,
    )
    monkeypatch.setitem(sys.modules, 'app.database.crud.yookassa', stub_module)

    user = types.SimpleNamespace(id=1)
    query = MiniAppPaymentStatusQuery(
        method='yookassa',
        localPaymentId=55,
        paymentId='yk_1',
        amountKopeks=15000,
        startedAt='2024-01-01T00:00:00Z',
        payload='payload123',
    )

    result = await miniapp._resolve_yookassa_payment_status(db=None, user=user, query=query)

    assert result.extra['local_payment_id'] == 55
    assert result.extra['payment_id'] == 'yk_1'
    assert result.extra['invoice_id'] == 'yk_1'
    assert result.extra['payload'] == 'payload123'
    assert result.extra['started_at'] == '2024-01-01T00:00:00Z'


@pytest.mark.anyio('asyncio')
async def test_resolve_payment_status_supports_yookassa_sbp(monkeypatch):
    payment = types.SimpleNamespace(
        id=77,
        user_id=5,
        amount_kopeks=25000,
        currency='RUB',
        status='pending',
        is_paid=False,
        captured_at=None,
        updated_at=None,
        created_at=datetime.now(UTC),
        transaction_id=None,
        yookassa_payment_id='yk_sbp_1',
    )

    async def fake_get_by_local_id(db, local_id):
        return payment if local_id == 77 else None

    async def fake_get_by_id(db, payment_id):
        return None

    stub_module = types.SimpleNamespace(
        get_yookassa_payment_by_local_id=fake_get_by_local_id,
        get_yookassa_payment_by_id=fake_get_by_id,
    )
    monkeypatch.setitem(sys.modules, 'app.database.crud.yookassa', stub_module)

    user = types.SimpleNamespace(id=5)
    query = MiniAppPaymentStatusQuery(
        method='yookassa_sbp',
        localPaymentId=77,
        amountKopeks=25000,
        startedAt='2024-05-01T10:00:00Z',
        payload='sbp_payload',
    )

    result = await miniapp._resolve_payment_status_entry(
        payment_service=types.SimpleNamespace(),
        db=None,
        user=user,
        query=query,
    )

    assert result.method == 'yookassa_sbp'
    assert result.status == 'pending'
    assert result.extra['local_payment_id'] == 77
    assert result.extra['payment_id'] == 'yk_sbp_1'
    assert result.extra['payload'] == 'sbp_payload'
    assert result.extra['started_at'] == '2024-05-01T10:00:00Z'


@pytest.mark.anyio('asyncio')
async def test_resolve_pal24_status_includes_identifiers(monkeypatch):
    async def fake_get_pal24_payment_by_bill_id(db, bill_id):
        return None

    stub_module = types.SimpleNamespace(
        get_pal24_payment_by_bill_id=fake_get_pal24_payment_by_bill_id,
    )
    monkeypatch.setitem(sys.modules, 'app.database.crud.pal24', stub_module)

    paid_at = datetime.now(UTC)

    payment = types.SimpleNamespace(
        id=321,
        user_id=1,
        amount_kopeks=25000,
        currency='RUB',
        is_paid=True,
        status='PAID',
        paid_at=paid_at,
        updated_at=paid_at,
        created_at=paid_at - timedelta(minutes=1),
        transaction_id=777,
        bill_id='BILL99',
        order_id='ORD99',
        payment_method='sbp',
    )

    class StubPal24Service:
        async def get_pal24_payment_status(self, db, local_id):
            assert local_id == 321
            return {
                'payment': payment,
                'status': 'PAID',
                'remote_status': 'PAID',
            }

    user = types.SimpleNamespace(id=1)
    query = MiniAppPaymentStatusQuery(
        method='pal24',
        localPaymentId=321,
        amountKopeks=25000,
        startedAt='2024-01-01T00:00:00Z',
        payload='pal24_payload',
    )

    result = await miniapp._resolve_pal24_payment_status(
        StubPal24Service(),
        db=None,
        user=user,
        query=query,
    )

    assert result.status == 'paid'
    assert result.extra['local_payment_id'] == 321
    assert result.extra['bill_id'] == 'BILL99'
    assert result.extra['order_id'] == 'ORD99'
    assert result.extra['payment_method'] == 'sbp'
    assert result.extra['payload'] == 'pal24_payload'
    assert result.extra['started_at'] == '2024-01-01T00:00:00Z'
    assert result.extra['remote_status'] == 'PAID'


@pytest.mark.anyio('asyncio')
async def test_resolve_wata_payment_status_success():
    paid_at = datetime.now(UTC)
    payment = types.SimpleNamespace(
        id=404,
        user_id=9,
        amount_kopeks=30000,
        currency='RUB',
        status='Paid',
        is_paid=True,
        payment_link_id='wata_link_404',
        order_id='order_404',
        transaction_id=909,
        paid_at=paid_at,
        updated_at=paid_at,
        created_at=paid_at - timedelta(minutes=1),
    )

    class StubWataService:
        async def get_wata_payment_status(self, db, local_payment_id):
            assert local_payment_id == 404
            return {
                'payment': payment,
                'status': 'Paid',
                'is_paid': True,
                'remote_link': {'status': 'Paid'},
                'transaction': {'id': 'tx_404', 'status': 'Paid'},
            }

    query = MiniAppPaymentStatusQuery(
        method='wata',
        localPaymentId=404,
        amountKopeks=30000,
        startedAt='2024-06-01T12:00:00Z',
        payload='wata_payload',
    )

    result = await miniapp._resolve_wata_payment_status(
        StubWataService(),
        db=None,
        user=types.SimpleNamespace(id=9),
        query=query,
    )

    assert result.status == 'paid'
    assert result.external_id == 'wata_link_404'
    assert result.extra['payment_link_id'] == 'wata_link_404'
    assert result.extra['transaction']['id'] == 'tx_404'
    assert result.extra['payload'] == 'wata_payload'
    assert result.extra['started_at'] == '2024-06-01T12:00:00Z'


@pytest.mark.anyio('asyncio')
async def test_resolve_wata_payment_status_uses_payment_link_lookup(monkeypatch):
    created_at = datetime.now(UTC)
    payment = types.SimpleNamespace(
        id=505,
        user_id=7,
        amount_kopeks=15000,
        currency='RUB',
        status='Opened',
        is_paid=False,
        payment_link_id='wata_lookup',
        order_id='order_lookup',
        transaction_id=None,
        paid_at=None,
        updated_at=None,
        created_at=created_at,
    )

    async def fake_get_wata_payment_by_link_id(db, link_id):
        assert link_id == 'wata_lookup'
        return payment

    monkeypatch.setattr(miniapp, 'get_wata_payment_by_link_id', fake_get_wata_payment_by_link_id)

    class StubWataService:
        async def get_wata_payment_status(self, db, local_payment_id):
            assert local_payment_id == 505
            return {
                'payment': payment,
                'status': payment.status,
                'is_paid': False,
                'remote_link': None,
                'transaction': None,
            }

    query = MiniAppPaymentStatusQuery(
        method='wata',
        paymentLinkId='wata_lookup',
        amountKopeks=15000,
    )

    result = await miniapp._resolve_wata_payment_status(
        StubWataService(),
        db=None,
        user=types.SimpleNamespace(id=7),
        query=query,
    )

    assert result.status == 'pending'
    assert result.extra['local_payment_id'] == 505
    assert result.extra['payment_link_id'] == 'wata_lookup'
    assert 'transaction' not in result.extra


@pytest.mark.anyio('asyncio')
async def test_create_payment_link_stars_normalizes_amount(monkeypatch):
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1000.0, raising=False)
    monkeypatch.setattr(settings, 'BOT_TOKEN', 'test-token', raising=False)

    captured = {}

    class DummyPaymentService:
        def __init__(self, bot):
            captured['bot'] = bot

        async def create_stars_invoice(
            self,
            amount_kopeks,
            description,
            payload,
            *,
            stars_amount=None,
        ):
            captured['amount_kopeks'] = amount_kopeks
            captured['description'] = description
            captured['payload'] = payload
            captured['stars_amount'] = stars_amount
            return 'https://invoice.example'

    class DummySession:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True
            captured['session_closed'] = True

    class DummyBot:
        def __init__(self, token):
            captured['bot_token'] = token
            self.session = DummySession()

    async def fake_resolve_user(db, init_data):
        return types.SimpleNamespace(id=7, language='ru'), {}

    monkeypatch.setattr(miniapp, 'PaymentService', DummyPaymentService)
    monkeypatch.setattr(miniapp, 'Bot', DummyBot)
    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', fake_resolve_user)

    payload = MiniAppPaymentCreateRequest(
        initData='data',
        method='stars',
        amountKopeks=101000,
    )

    response = await miniapp.create_payment_link(payload, db=types.SimpleNamespace())

    assert response.payment_url == 'https://invoice.example'
    assert response.amount_kopeks == 100000
    assert response.extra['stars_amount'] == 1
    assert response.extra['requested_amount_kopeks'] == 101000
    assert captured['amount_kopeks'] == 100000
    assert captured['stars_amount'] == 1
    assert captured['bot_token'] == 'test-token'
    assert captured.get('session_closed') is True


@pytest.mark.anyio('asyncio')
async def test_get_payment_methods_exposes_stars_min_amount(monkeypatch):
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 999.99, raising=False)

    async def fake_resolve_user(db, init_data):
        return types.SimpleNamespace(id=1, language='ru'), {}

    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', fake_resolve_user)

    payload = MiniAppPaymentMethodsRequest(initData='abc')

    response = await miniapp.get_payment_methods(payload, db=types.SimpleNamespace())

    stars_method = next((method for method in response.methods if method.id == 'stars'), None)
    assert stars_method is not None
    assert stars_method.min_amount_kopeks == 99999
    assert stars_method.amount_step_kopeks == 99999
    assert stars_method.integration_type == MiniAppPaymentIntegrationType.REDIRECT
    assert stars_method.iframe_config is None


@pytest.mark.anyio('asyncio')
async def test_get_payment_methods_includes_wata(monkeypatch):
    monkeypatch.setattr(settings, 'WATA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'WATA_ACCESS_TOKEN', 'token', raising=False)
    monkeypatch.setattr(settings, 'WATA_TERMINAL_PUBLIC_ID', 'terminal', raising=False)
    monkeypatch.setattr(settings, 'WATA_MIN_AMOUNT_KOPEKS', 5000, raising=False)
    monkeypatch.setattr(settings, 'WATA_MAX_AMOUNT_KOPEKS', 7500000, raising=False)

    async def fake_resolve_user(db, init_data):
        return types.SimpleNamespace(id=1, language='ru'), {}

    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', fake_resolve_user)

    payload = MiniAppPaymentMethodsRequest(initData='abc')
    response = await miniapp.get_payment_methods(payload, db=types.SimpleNamespace())

    wata_method = next((method for method in response.methods if method.id == 'wata'), None)

    assert wata_method is not None
    assert wata_method.min_amount_kopeks == 5000
    assert wata_method.max_amount_kopeks == 7500000
    assert wata_method.icon == '🌊'
    assert wata_method.integration_type == MiniAppPaymentIntegrationType.REDIRECT
    assert wata_method.iframe_config is None


@pytest.mark.anyio('asyncio')
async def test_get_payment_methods_marks_mulenpay_iframe(monkeypatch):
    monkeypatch.setattr(settings, 'MULENPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_API_KEY', 'api-key', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SECRET_KEY', 'secret', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_SHOP_ID', 99, raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_BASE_URL', 'https://checkout.example/api', raising=False)
    monkeypatch.setattr(settings, 'MULENPAY_IFRAME_EXPECTED_ORIGIN', None, raising=False)

    async def fake_resolve_user(db, init_data):
        return types.SimpleNamespace(id=1, language='ru'), {}

    monkeypatch.setattr(miniapp, '_resolve_user_from_init_data', fake_resolve_user)

    payload = MiniAppPaymentMethodsRequest(initData='abc')
    response = await miniapp.get_payment_methods(payload, db=types.SimpleNamespace())

    mulenpay_method = next((method for method in response.methods if method.id == 'mulenpay'), None)
    assert mulenpay_method is not None
    assert mulenpay_method.integration_type == MiniAppPaymentIntegrationType.IFRAME
    assert mulenpay_method.iframe_config is not None
    assert str(mulenpay_method.iframe_config.expected_origin) == 'https://checkout.example'


@pytest.mark.anyio('asyncio')
async def test_find_recent_deposit_ignores_transactions_before_attempt():
    started_at = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)

    transaction = types.SimpleNamespace(
        id=10,
        amount_kopeks=1000,
        completed_at=None,
        created_at=started_at - timedelta(minutes=1),
    )

    class DummyResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class DummySession:
        def __init__(self, value):
            self._value = value

        async def execute(self, query):
            return DummyResult(self._value)

    result = await miniapp._find_recent_deposit(
        DummySession(transaction),
        user_id=1,
        payment_method=PaymentMethod.TELEGRAM_STARS,
        amount_kopeks=1000,
        started_at=started_at,
    )

    assert result is None


@pytest.mark.anyio('asyncio')
async def test_find_recent_deposit_accepts_recent_transactions():
    started_at = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)

    transaction = types.SimpleNamespace(
        id=11,
        amount_kopeks=1000,
        completed_at=started_at + timedelta(seconds=5),
        created_at=started_at + timedelta(seconds=5),
    )

    class DummyResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class DummySession:
        def __init__(self, value):
            self._value = value

        async def execute(self, query):
            return DummyResult(self._value)

    result = await miniapp._find_recent_deposit(
        DummySession(transaction),
        user_id=1,
        payment_method=PaymentMethod.TELEGRAM_STARS,
        amount_kopeks=1000,
        started_at=started_at,
    )

    assert result is transaction
