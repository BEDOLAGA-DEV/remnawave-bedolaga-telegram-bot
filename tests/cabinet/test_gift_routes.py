"""Unit and contract tests for cabinet gift routes and branding feature toggle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.cabinet.routes import branding as branding_routes, gift as gift_routes
from app.cabinet.routes.branding import GiftEnabledUpdate
from app.cabinet.schemas.gift import GiftPurchaseRequest
from app.database.models import (
    DiscountOffer,
    GuestPurchase,
    GuestPurchaseStatus,
    PaymentMethod,
    PaymentMethodConfig,
    PromoGroup,
    PromoOfferLog,
    Subscription,
    SystemSetting,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserPromoGroup,
    Webhook,
    tariff_promo_groups,
)
from app.services.gift_purchase_service import GIFT_ENABLED_KEY, is_gift_enabled
from tests.fixtures.sqlite_memory import memory_session


_TABLES = [
    SystemSetting.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    Subscription.__table__,
    User.__table__,
    GuestPurchase.__table__,
    Transaction.__table__,
    DiscountOffer.__table__,
    PromoOfferLog.__table__,
    PaymentMethodConfig.__table__,
    Webhook.__table__,
]


@pytest.fixture(autouse=True)
def bypass_rate_limit(monkeypatch):
    """Disable rate limiting for route tests."""
    from app.utils.cache import RateLimitCache

    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))


# ── Step 1: Feature Switch and Branding Routes ─────────────────────────────


@pytest.mark.asyncio
async def test_branding_gift_enabled_routes(monkeypatch):
    """get_gift_enabled and update_gift_enabled in branding read/write the shared setting."""
    async with memory_session(monkeypatch, _TABLES) as db:
        # Default: absent setting -> disabled
        res1 = await branding_routes.get_gift_enabled(db=db)
        assert res1.enabled is False
        assert await is_gift_enabled(db) is False

        # Admin enables gift feature
        admin = User(id=1, telegram_id=12345, username='admin')
        res2 = await branding_routes.update_gift_enabled(
            payload=GiftEnabledUpdate(enabled=True),
            admin=admin,
            db=db,
        )
        assert res2.enabled is True
        assert await is_gift_enabled(db) is True

        # Public get returns enabled
        res3 = await branding_routes.get_gift_enabled(db=db)
        assert res3.enabled is True

        # Admin disables gift feature
        res4 = await branding_routes.update_gift_enabled(
            payload=GiftEnabledUpdate(enabled=False),
            admin=admin,
            db=db,
        )
        assert res4.enabled is False
        assert await is_gift_enabled(db) is False


# ── Step 2: /gift/config Route Tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gift_config_when_disabled(monkeypatch):
    """When gift feature is disabled, /gift/config returns is_enabled=False and user balance."""
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, balance_kopeks=50000, username='buyer')
        db.add(user)
        await db.commit()

        config = await gift_routes.get_gift_config(user=user, db=db)
        assert config.is_enabled is False
        assert config.balance_kopeks == 50000
        assert config.tariffs == []


@pytest.mark.asyncio
async def test_gift_config_filters_tariffs_and_orders(monkeypatch):
    """Only active tariffs with show_in_gift=True are returned, ordered by display_order then id."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        db.add(user)

        # 1. Eligible, display_order 2
        t1 = Tariff(
            id=1,
            name='Tariff Beta',
            is_active=True,
            show_in_gift=True,
            display_order=2,
            period_prices={'30': 30000},
            device_limit=2,
            traffic_limit_gb=50,
        )
        # 2. Eligible, display_order 1 (should appear first)
        t2 = Tariff(
            id=2,
            name='Tariff Alpha',
            is_active=True,
            show_in_gift=True,
            display_order=1,
            period_prices={'30': 20000, '90': 50000},
            device_limit=1,
            traffic_limit_gb=20,
        )
        # 3. Inactive (should be excluded)
        t3 = Tariff(
            id=3,
            name='Tariff Inactive',
            is_active=False,
            show_in_gift=True,
            display_order=0,
            period_prices={'30': 10000},
        )
        # 4. show_in_gift=False (should be excluded)
        t4 = Tariff(
            id=4,
            name='Tariff No Gift',
            is_active=True,
            show_in_gift=False,
            display_order=0,
            period_prices={'30': 10000},
        )
        db.add_all([t1, t2, t3, t4])
        await db.commit()

        config = await gift_routes.get_gift_config(user=user, db=db)
        assert config.is_enabled is True
        assert [t.id for t in config.tariffs] == [2, 1]
        assert config.tariffs[0].name == 'Tariff Alpha'
        assert len(config.tariffs[0].periods) == 2
        assert config.tariffs[0].periods[0].days == 30
        assert config.tariffs[0].periods[0].price_kopeks == 20000
        assert config.tariffs[0].periods[1].days == 90
        assert config.tariffs[0].periods[1].price_kopeks == 50000


@pytest.mark.asyncio
async def test_gift_config_personalized_quote_fields(monkeypatch):
    """Personalized discounts (promo group & active promo offer) populate quote fields."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(
            id=10,
            balance_kopeks=50000,
            username='buyer',
            promo_offer_discount_percent=20,
        )
        db.add(user)

        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            display_order=1,
            period_prices={'30': 10000},
            device_limit=1,
            traffic_limit_gb=30,
        )
        db.add(tariff)
        await db.commit()

        config = await gift_routes.get_gift_config(user=user, db=db)
        assert config.is_enabled is True
        assert config.active_discount_percent == 20
        period = config.tariffs[0].periods[0]
        assert period.days == 30
        assert period.price_kopeks == 8000
        assert period.original_price_kopeks == 10000
        assert period.discount_percent == 20


# ── Step 3: /gift/purchase Balance Mode Tests ──────────────────────────────


@pytest.mark.asyncio
async def test_purchase_gift_balance_success(monkeypatch):
    """Balance checkout creates a paid GuestPurchase with cabinet idempotency and debits balance."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer', email='buyer@example.com')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
            device_limit=1,
        )
        db.add_all([user, tariff])
        await db.commit()

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='balance',
            gift_message='Enjoy your subscription!',
        )
        response = await gift_routes.create_gift_purchase(body=req, user=user, db=db)

        assert response.status == 'ok'
        assert len(response.purchase_token) == 12

        # Check DB state
        res = await db.execute(select(GuestPurchase).where(GuestPurchase.buyer_user_id == 10))
        purchase = res.scalars().first()
        assert purchase is not None
        assert purchase.status == GuestPurchaseStatus.PAID.value
        assert purchase.amount_kopeks == 30000
        assert purchase.is_gift is True
        assert purchase.source == 'cabinet'
        assert purchase.idempotency_key is not None
        assert purchase.idempotency_key.startswith('cab_')
        assert purchase.gift_message == 'Enjoy your subscription!'
        assert purchase.gift_recipient_type is None
        assert purchase.gift_recipient_value is None

        # Check user balance
        await db.refresh(user)
        assert user.balance_kopeks == 20000

        # Check transaction
        tx_res = await db.execute(select(Transaction).where(Transaction.user_id == 10))
        tx = tx_res.scalars().first()
        assert tx is not None
        assert tx.type == TransactionType.GIFT_PAYMENT.value
        assert tx.payment_method == PaymentMethod.BALANCE.value
        assert abs(tx.amount_kopeks) == 30000


@pytest.mark.asyncio
async def test_purchase_gift_balance_directed_and_notification(monkeypatch):
    """Directed gift persists recipient details and invokes claim notification."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer', email='buyer@example.com')
        tariff = Tariff(
            id=1,
            name='Pro',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        notify_mock = AsyncMock()
        monkeypatch.setattr('app.cabinet.routes.gift.notify_gift_claim_available', notify_mock)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            recipient_type='email',
            recipient_value='friend@example.com',
            gift_message='Happy Birthday!',
            payment_mode='balance',
        )
        response = await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert response.status == 'ok'

        res = await db.execute(select(GuestPurchase).where(GuestPurchase.buyer_user_id == 10))
        purchase = res.scalars().first()
        assert purchase is not None
        assert purchase.gift_recipient_type == 'email'
        assert purchase.gift_recipient_value == 'friend@example.com'
        assert purchase.gift_message == 'Happy Birthday!'

        notify_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_purchase_gift_balance_insufficient_balance(monkeypatch):
    """When balance is insufficient, raises 400 Insufficient balance."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=5000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='balance',
        )
        with pytest.raises(HTTPException) as exc_info:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == 'Insufficient balance'

        # User balance unchanged
        await db.refresh(user)
        assert user.balance_kopeks == 5000


@pytest.mark.asyncio
async def test_purchase_gift_restricted_user(monkeypatch):
    """Restricted buyer receives 403 Forbidden."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer', restriction_subscription=True)
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='balance',
        )
        with pytest.raises(HTTPException) as exc_info:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == 'Purchases are restricted for this account'


@pytest.mark.asyncio
async def test_purchase_gift_disabled_feature(monkeypatch):
    """When gift feature is disabled, purchase raises 400 Gift feature is not enabled."""
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='balance',
        )
        with pytest.raises(HTTPException) as exc_info:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == 'Gift feature is not enabled'


@pytest.mark.asyncio
async def test_purchase_gift_tariff_not_found_or_inactive(monkeypatch):
    """Inactive or non-gift tariffs raise 404 Tariff not found or inactive."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=False,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        # Inactive tariff
        req = GiftPurchaseRequest(tariff_id=1, period_days=30, payment_mode='balance')
        with pytest.raises(HTTPException) as exc_info:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == 'Tariff not found or inactive'

        # Non-existent tariff
        req_missing = GiftPurchaseRequest(tariff_id=999, period_days=30, payment_mode='balance')
        with pytest.raises(HTTPException) as exc_info2:
            await gift_routes.create_gift_purchase(body=req_missing, user=user, db=db)
        assert exc_info2.value.status_code == 404
        assert exc_info2.value.detail == 'Tariff not found or inactive'


@pytest.mark.asyncio
async def test_purchase_gift_invalid_period(monkeypatch):
    """Requesting unconfigured period raises 400 Price is not configured for this period."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        req = GiftPurchaseRequest(tariff_id=1, period_days=90, payment_mode='balance')
        with pytest.raises(HTTPException) as exc_info:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == 'Price is not configured for this period'


@pytest.mark.asyncio
async def test_purchase_gift_self_gift_prevention(monkeypatch):
    """Self-gifting by username or email raises 400 Cannot gift to yourself."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='myname', email='me@example.com')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        # Self-gift via telegram
        req_tg = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            recipient_type='telegram',
            recipient_value='@MYNAME',
            payment_mode='balance',
        )
        with pytest.raises(HTTPException) as exc_tg:
            await gift_routes.create_gift_purchase(body=req_tg, user=user, db=db)
        assert exc_tg.value.status_code == 400
        assert exc_tg.value.detail == 'Cannot gift to yourself'

        # Self-gift via email
        req_em = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            recipient_type='email',
            recipient_value='ME@EXAMPLE.COM',
            payment_mode='balance',
        )
        with pytest.raises(HTTPException) as exc_em:
            await gift_routes.create_gift_purchase(body=req_em, user=user, db=db)
        assert exc_em.value.status_code == 400
        assert exc_em.value.detail == 'Cannot gift to yourself'


# ── Step 4: /gift/purchase Gateway Mode Tests ──────────────────────────────


@pytest.mark.asyncio
async def test_purchase_gift_gateway_success(monkeypatch):
    """Gateway mode creates a payment via PaymentService and does not debit user balance."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer', email='buyer@example.com')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        fake_payment_service = MagicMock()
        fake_payment_service.create_guest_payment = AsyncMock(
            return_value={'payment_url': 'https://pay.provider.example/checkout/123'}
        )
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_payment_service)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        response = await gift_routes.create_gift_purchase(body=req, user=user, db=db)

        assert response.status == 'created'
        assert response.payment_url == 'https://pay.provider.example/checkout/123'
        assert len(response.purchase_token) == 12

        # Verify GuestPurchase in DB
        res = await db.execute(select(GuestPurchase).where(GuestPurchase.buyer_user_id == 10))
        purchase = res.scalars().first()
        assert purchase is not None
        assert purchase.payment_method == 'yookassa'
        assert purchase.status == GuestPurchaseStatus.PENDING.value
        assert purchase.amount_kopeks == 30000

        # Balance was NOT debited
        await db.refresh(user)
        assert user.balance_kopeks == 50000


@pytest.mark.asyncio
async def test_purchase_gift_gateway_provider_error(monkeypatch):
    """When payment provider returns None, raises 502 Bad Gateway."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        fake_svc = MagicMock()
        fake_svc.create_guest_payment = AsyncMock(return_value=None)
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_svc)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        with pytest.raises(HTTPException) as exc:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert exc.value.status_code == 502
        assert exc.value.detail == 'Payment provider is unavailable, please try again later'


@pytest.mark.asyncio
async def test_purchase_gift_gateway_invalid_response(monkeypatch):
    """When payment provider returns no payment_url, raises 502 Bad Gateway."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        fake_svc = MagicMock()
        fake_svc.create_guest_payment = AsyncMock(return_value={})
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_svc)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        with pytest.raises(HTTPException) as exc:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert exc.value.status_code == 502
        assert exc.value.detail == 'Payment provider returned an invalid response'


@pytest.mark.asyncio
async def test_purchase_gift_telegram_unresolvable_warning(monkeypatch):
    """When a recipient telegram username is not in DB and unresolvable via Bot API, warning is returned."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            recipient_type='telegram',
            recipient_value='@unknown_recipient_user',
            payment_mode='balance',
        )
        response = await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert response.status == 'ok'
        assert response.warning == 'telegram_unresolvable'


@pytest.mark.asyncio
async def test_purchase_gift_gateway_consumes_one_time_promo_offer(monkeypatch):
    """P1 Gateway Discount: personal one-time promo discount is applied and consumed on successful payment creation."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(
            id=10,
            balance_kopeks=50000,
            username='buyer',
            promo_offer_discount_percent=20,
            promo_offer_discount_source='personal_test',
        )
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        captured_amount = None

        async def fake_create_payment(**kwargs):
            nonlocal captured_amount
            captured_amount = kwargs.get('amount_kopeks')
            return {'payment_url': 'https://pay.example.com/inv_123', 'provider': 'yookassa'}

        fake_svc = MagicMock()
        fake_svc.create_guest_payment = AsyncMock(side_effect=fake_create_payment)
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_svc)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        response = await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert response.status == 'created'
        assert captured_amount == 24000  # 30000 - 20%

        # Verify promo offer is consumed in DB
        db_user = await db.get(User, user.id)
        assert db_user.promo_offer_discount_percent == 0
        assert db_user.promo_offer_discount_source is None


@pytest.mark.asyncio
async def test_purchase_gift_gateway_provider_error_rolls_back_promo_offer(monkeypatch):
    """P1 Gateway Discount: provider failure or exception cleanly rolls back promo offer consumption."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user_id = 10
        user = User(
            id=user_id,
            balance_kopeks=50000,
            username='buyer',
            promo_offer_discount_percent=20,
            promo_offer_discount_source='personal_test',
        )
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        fake_svc = MagicMock()
        fake_svc.create_guest_payment = AsyncMock(return_value=None)  # Provider failure
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_svc)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        with pytest.raises(HTTPException) as exc:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert exc.value.status_code == 502

        # Verify promo offer was NOT consumed
        row = (
            await db.execute(
                select(User.promo_offer_discount_percent, User.promo_offer_discount_source).where(User.id == user_id)
            )
        ).one()
        assert row[0] == 20
        assert row[1] == 'personal_test'


@pytest.mark.asyncio
async def test_purchase_gift_gateway_internal_adapter_commit_is_deferred_until_url_validation(monkeypatch):
    """An adapter commit must not persist promo consumption before the route validates its result."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user_id = 10
        user = User(
            id=user_id,
            balance_kopeks=50000,
            username='buyer',
            promo_offer_discount_percent=20,
            promo_offer_discount_source='personal_test',
        )
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        async def fake_adapter_with_internal_commit(**kwargs):
            # Real provider CRUD helpers call commit after writing the local
            # payment. The gift route must defer that commit to a flush.
            await kwargs['db'].commit()
            return {'payment_url': None, 'provider': 'broken-provider'}

        fake_svc = MagicMock()
        fake_svc.create_guest_payment = AsyncMock(side_effect=fake_adapter_with_internal_commit)
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_svc)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        with pytest.raises(HTTPException) as exc:
            await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert exc.value.status_code == 502

        promo_row = (
            await db.execute(
                select(User.promo_offer_discount_percent, User.promo_offer_discount_source).where(User.id == user_id)
            )
        ).one()
        assert promo_row == (20, 'personal_test')

        purchase_count = await db.scalar(select(func.count()).select_from(GuestPurchase))
        assert purchase_count == 0


@pytest.mark.asyncio
async def test_purchase_gift_gateway_concurrent_requests_apply_promo_at_most_once(monkeypatch):
    """P1 Gateway Concurrency: when 2 gateway requests run with 1 personal discount,
    discount is applied at most once (1st gets discounted, 2nd gets full price)."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(
            id=10,
            balance_kopeks=50000,
            username='buyer',
            promo_offer_discount_percent=20,
            promo_offer_discount_source='personal_test',
        )
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        captured_amounts = []

        async def fake_create_payment(**kwargs):
            captured_amounts.append(kwargs.get('amount_kopeks'))
            return {'payment_url': f'https://pay.example.com/inv_{len(captured_amounts)}', 'provider': 'yookassa'}

        fake_svc = MagicMock()
        fake_svc.create_guest_payment = AsyncMock(side_effect=fake_create_payment)
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_svc)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )

        # 1st request
        res1 = await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert res1.status == 'created'

        # 2nd request (with same user)
        res2 = await gift_routes.create_gift_purchase(body=req, user=user, db=db)
        assert res2.status == 'created'

        # Verify amounts: 1st got 20% discount (24000), 2nd got full price (30000)
        assert captured_amounts == [24000, 30000]


# ── Step 5: Canonical Gift Field Parity and Response Contract Tests ───────


@pytest.mark.asyncio
async def test_purchase_gift_balance_returns_canonical_fields(monkeypatch):
    """Balance gift purchase returns additive canonical gift_code, bot_claim_url, cabinet_claim_url."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
            device_limit=1,
        )
        db.add_all([user, tariff])
        await db.commit()

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='balance',
            gift_message='Enjoy!',
        )
        response = await gift_routes.create_gift_purchase(body=req, user=user, db=db)

        assert response.status == 'ok'
        assert len(response.purchase_token) == 12

        # Check DB to get the full token
        res = await db.execute(select(GuestPurchase).where(GuestPurchase.buyer_user_id == 10))
        purchase = res.scalars().first()
        assert purchase is not None
        assert purchase.token.startswith(response.purchase_token)

        # Canonical fields match Task 1 derivations
        expected_code = f'GIFT_{purchase.token[:59]}'
        assert response.gift_code == expected_code
        assert response.bot_claim_url == f'https://t.me/test_vpn_bot?start={expected_code}'
        assert response.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{purchase.token}'


@pytest.mark.asyncio
async def test_purchase_gift_gateway_pending_has_no_claim_fields(monkeypatch):
    """Gateway gift purchase returns null claim fields while in pending state."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))
        user = User(id=10, balance_kopeks=50000, username='buyer')
        tariff = Tariff(
            id=1,
            name='Standard',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([user, tariff])
        await db.commit()

        fake_svc = MagicMock()
        fake_svc.create_guest_payment = AsyncMock(
            return_value={'payment_url': 'https://pay.provider.example/checkout/123'}
        )
        monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_svc)

        req = GiftPurchaseRequest(
            tariff_id=1,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        response = await gift_routes.create_gift_purchase(body=req, user=user, db=db)

        assert response.status == 'created'
        assert response.payment_url == 'https://pay.provider.example/checkout/123'
        assert len(response.purchase_token) == 12
        assert response.gift_code is None
        assert response.bot_claim_url is None
        assert response.cabinet_claim_url is None


@pytest.mark.asyncio
async def test_get_gift_purchase_status_pending_has_no_claim_fields(monkeypatch):
    """Pending purchase status returns is_claimable=False and no claim credentials."""
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, username='buyer')
        tariff = Tariff(id=1, name='Pro', device_limit=2)
        full_token = 'p' * 64
        purchase = GuestPurchase(
            id=1,
            token=full_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=30000,
            is_gift=True,
            status=GuestPurchaseStatus.PENDING.value,
            buyer_user_id=10,
        )
        db.add_all([user, tariff, purchase])
        await db.commit()

        res = await gift_routes.get_gift_purchase_status(token=full_token, user=user, db=db)
        assert res.status == 'pending'
        assert res.is_claimable is False
        assert res.purchase_token is None
        assert res.gift_code is None
        assert res.bot_claim_url is None
        assert res.cabinet_claim_url is None


@pytest.mark.asyncio
async def test_get_gift_purchase_status_paid_code_only(monkeypatch):
    """Paid code-only gift status returns canonical code and links with legacy 12-char token."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, username='buyer')
        tariff = Tariff(id=1, name='Standard', device_limit=1)
        full_token = 'k' * 64
        purchase = GuestPurchase(
            id=1,
            token=full_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
            gift_message='Happy holidays!',
        )
        db.add_all([user, tariff, purchase])
        await db.commit()

        # Query using 12-char prefix
        res = await gift_routes.get_gift_purchase_status(token=full_token[:12], user=user, db=db)
        assert res.status == 'paid'
        assert res.is_claimable is True
        assert res.is_code_only is True
        assert res.purchase_token == full_token[:12]
        assert res.gift_code == f'GIFT_{full_token[:59]}'
        assert res.bot_claim_url == f'https://t.me/test_vpn_bot?start=GIFT_{full_token[:59]}'
        assert res.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{full_token}'
        assert res.gift_message == 'Happy holidays!'


@pytest.mark.asyncio
async def test_get_gift_purchase_status_directed_gift(monkeypatch):
    """Directed gift status populates recipient value and claim artifacts for the buyer."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, username='buyer')
        tariff = Tariff(id=1, name='Standard', device_limit=1)
        full_token = 'd' * 64
        purchase = GuestPurchase(
            id=1,
            token=full_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=60,
            amount_kopeks=40000,
            is_gift=True,
            gift_recipient_type='email',
            gift_recipient_value='friend@example.com',
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
        )
        db.add_all([user, tariff, purchase])
        await db.commit()

        res = await gift_routes.get_gift_purchase_status(token=full_token, user=user, db=db)
        assert res.status == 'paid'
        assert res.is_claimable is True
        assert res.is_code_only is False
        assert res.recipient_contact_value == 'friend@example.com'
        assert res.purchase_token == full_token[:12]
        assert res.gift_code == f'GIFT_{full_token[:59]}'
        assert res.bot_claim_url == f'https://t.me/test_vpn_bot?start=GIFT_{full_token[:59]}'
        assert res.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{full_token}'


@pytest.mark.asyncio
async def test_get_gift_purchase_status_delivered_has_no_claim_actions(monkeypatch):
    """Delivered gift retains metadata but exposes no reusable claim actions/links."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = User(id=10, username='buyer')
        recipient = User(id=20, username='recipient')
        tariff = Tariff(id=1, name='Standard', device_limit=1)
        full_token = 'x' * 64
        purchase = GuestPurchase(
            id=1,
            token=full_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.DELIVERED.value,
            buyer_user_id=10,
            user_id=20,
        )
        db.add_all([buyer, recipient, tariff, purchase])
        await db.commit()

        res = await gift_routes.get_gift_purchase_status(token=full_token, user=buyer, db=db)
        assert res.status == 'delivered'
        assert res.is_claimable is False
        assert res.purchase_token is None
        assert res.gift_code is None
        assert res.bot_claim_url is None
        assert res.cabinet_claim_url is None


@pytest.mark.asyncio
async def test_get_gift_purchase_status_uniform_404_for_non_buyer(monkeypatch):
    """Querying another buyer's purchase token raises 404 to avoid token existence oracle."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = User(id=10, username='buyer')
        stranger = User(id=99, username='stranger')
        tariff = Tariff(id=1, name='Standard')
        full_token = 's' * 64
        purchase = GuestPurchase(
            id=1,
            token=full_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
        )
        db.add_all([buyer, stranger, tariff, purchase])
        await db.commit()

        with pytest.raises(HTTPException) as exc:
            await gift_routes.get_gift_purchase_status(token=full_token, user=stranger, db=db)
        assert exc.value.status_code == 404
        assert exc.value.detail == 'Purchase not found'


@pytest.mark.asyncio
async def test_get_gift_purchase_status_absent_bot_or_cabinet_config(monkeypatch):
    """When bot username or cabinet URL is not configured, canonical code is still returned and missing URLs are None."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', None)
    monkeypatch.setattr(settings, 'BOT_USERNAME', None)
    monkeypatch.setattr(settings, 'BOT_TOKEN', None)
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, username='buyer')
        tariff = Tariff(id=1, name='Standard')
        full_token = 'c' * 64
        purchase = GuestPurchase(
            id=1,
            token=full_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
        )
        db.add_all([user, tariff, purchase])
        await db.commit()

        res = await gift_routes.get_gift_purchase_status(token=full_token, user=user, db=db)
        assert res.gift_code == f'GIFT_{full_token[:59]}'
        assert res.bot_claim_url is None
        assert res.cabinet_claim_url is None


@pytest.mark.asyncio
async def test_get_sent_gifts_contract_and_channel_parity(monkeypatch):
    """get_sent_gifts returns canonical claim fields for claimable gifts and omits them for delivered gifts."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = User(id=10, username='buyer')
        recipient = User(id=20, username='alice')
        tariff = Tariff(id=1, name='Standard', device_limit=2)
        db.add_all([buyer, recipient, tariff])

        # 1. Paid gift (claimable)
        t1 = '1' * 64
        p1 = GuestPurchase(
            id=1,
            token=t1,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
            source='cabinet',
            gift_message='For you!',
        )
        # 2. Delivered gift (claimed)
        t2 = '2' * 64
        p2 = GuestPurchase(
            id=2,
            token=t2,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=60,
            amount_kopeks=40000,
            is_gift=True,
            status=GuestPurchaseStatus.DELIVERED.value,
            buyer_user_id=10,
            user_id=20,
            source='cabinet',
        )
        db.add_all([p1, p2])
        await db.commit()

        sent = await gift_routes.get_sent_gifts(user=buyer, db=db)
        assert len(sent) == 2

        # Most recent first (id 2, then id 1)
        item_delivered = next(s for s in sent if s.status == 'delivered')
        item_paid = next(s for s in sent if s.status == 'paid')

        # Delivered item checks
        assert item_delivered.token == t2[:12]
        assert item_delivered.activated_by_username == '@alice'
        assert item_delivered.gift_code is None
        assert item_delivered.bot_claim_url is None
        assert item_delivered.cabinet_claim_url is None

        # Paid item checks
        assert item_paid.token == t1[:12]
        assert item_paid.gift_code == f'GIFT_{t1[:59]}'
        assert item_paid.bot_claim_url == f'https://t.me/test_vpn_bot?start=GIFT_{t1[:59]}'
        assert item_paid.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{t1}'
        assert item_paid.gift_message == 'For you!'


@pytest.mark.asyncio
async def test_get_sent_gifts_includes_bot_origin_gifts(monkeypatch):
    """Gifts purchased via Telegram bot appear in cabinet /gift/sent with canonical claim artifacts."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = User(id=10, telegram_id=111, username='bot_buyer')
        tariff = Tariff(id=1, name='VIP', device_limit=3)
        token_bot = 'b' * 64
        p_bot = GuestPurchase(
            id=1,
            token=token_bot,
            contact_type='telegram',
            contact_value='@bot_buyer',
            tariff_id=1,
            period_days=90,
            amount_kopeks=60000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
            source='bot',
        )
        db.add_all([buyer, tariff, p_bot])
        await db.commit()

        sent = await gift_routes.get_sent_gifts(user=buyer, db=db)
        assert len(sent) == 1
        item = sent[0]
        assert item.tariff_name == 'VIP'
        assert item.device_limit == 3
        assert item.gift_code == f'GIFT_{token_bot[:59]}'
        assert item.bot_claim_url == f'https://t.me/test_vpn_bot?start=GIFT_{token_bot[:59]}'
        assert item.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{token_bot}'


@pytest.mark.asyncio
async def test_landing_purchase_status_canonical_fields_parity(monkeypatch):
    """_build_purchase_status_response in landing routes includes additive canonical fields."""
    from app.cabinet.routes.landing import _build_purchase_status_response
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')

    full_token = 'l' * 64
    tariff = Tariff(id=1, name='Standard')
    purchase = GuestPurchase(
        id=1,
        token=full_token,
        contact_type='email',
        contact_value='buyer@example.com',
        tariff=tariff,
        tariff_id=1,
        period_days=30,
        amount_kopeks=20000,
        is_gift=True,
        status=GuestPurchaseStatus.PAID.value,
    )

    resp = _build_purchase_status_response(purchase)
    assert resp.is_claimable is True
    # Legacy fields preserved
    assert resp.claim_url == f'https://cabinet.example.com/buy/gift/{full_token}'
    assert resp.bot_claim_link == f'https://t.me/test_vpn_bot?start=GIFT_{full_token[:59]}'
    # Additive canonical fields
    assert resp.gift_code == f'GIFT_{full_token[:59]}'
    assert resp.bot_claim_url == f'https://t.me/test_vpn_bot?start=GIFT_{full_token[:59]}'
    assert resp.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{full_token}'


# ── Step 6: Historical Compatibility and Activation Parity Tests ───────────


@pytest.mark.asyncio
async def test_historical_gifts_derive_canonical_codes_without_migration(monkeypatch):
    """Historical gift rows in DB seamlessly derive canonical GIFT_<59> public codes without migration."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, username='buyer')
        tariff = Tariff(id=1, name='Standard')
        historical_token = 'historical_token_64_characters_abcdefghijklmnopqrstuvwxyz012345'
        p = GuestPurchase(
            id=1,
            token=historical_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
        )
        db.add_all([user, tariff, p])
        await db.commit()

        res = await gift_routes.get_gift_purchase_status(token=historical_token[:12], user=user, db=db)
        assert res.gift_code == f'GIFT_{historical_token[:59]}'
        assert res.bot_claim_url == f'https://t.me/test_vpn_bot?start=GIFT_{historical_token[:59]}'
        assert res.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{historical_token}'


@pytest.mark.asyncio
async def test_activate_gift_backward_compatibility_short_codes_and_canonical(monkeypatch):
    """Cabinet /gift/activate endpoint accepts 8-char, 12-char, GIFT- prefix, canonical GIFT_, and full URLs."""
    from app.cabinet.schemas.gift import ActivateGiftRequest

    fake_activate_svc = AsyncMock()
    monkeypatch.setattr('app.services.guest_purchase_service.activate_purchase', fake_activate_svc)

    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = User(id=10, username='buyer')
        claimant = User(id=20, username='claimant')
        tariff = Tariff(id=1, name='Standard')
        token = 'abcdefghij1234567890abcdefghijklmnopqrstuvwxyz0123456789abcdef01'
        purchase = GuestPurchase(
            id=1,
            token=token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
        )
        db.add_all([buyer, claimant, tariff, purchase])
        await db.commit()

        # 1. 8-char prefix
        res1 = await gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=token[:8]),
            user=claimant,
            db=db,
        )
        assert res1.status == 'activated'
        assert res1.tariff_name == 'Standard'
        assert res1.period_days == 30
        fake_activate_svc.assert_awaited()

        # Reset purchase for next test
        purchase.status = GuestPurchaseStatus.PAID.value
        purchase.user_id = None
        await db.commit()

        # 2. Legacy GIFT-<12 chars> format
        res2 = await gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=f'GIFT-{token[:12]}'),
            user=claimant,
            db=db,
        )
        assert res2.status == 'activated'

        # Reset purchase
        purchase.status = GuestPurchaseStatus.PAID.value
        purchase.user_id = None
        await db.commit()

        # 3. Canonical GIFT_<59 chars> code
        res3 = await gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=f'GIFT_{token[:59]}'),
            user=claimant,
            db=db,
        )
        assert res3.status == 'activated'

        # Reset purchase
        purchase.status = GuestPurchaseStatus.PAID.value
        purchase.user_id = None
        await db.commit()

        # 4. Telegram deep link URL
        res4 = await gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=f'https://t.me/test_bot?start=GIFT_{token[:59]}'),
            user=claimant,
            db=db,
        )
        assert res4.status == 'activated'

        # Reset purchase
        purchase.status = GuestPurchaseStatus.PAID.value
        purchase.user_id = None
        await db.commit()

        # 5. Web cabinet URL
        res5 = await gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=f'https://cabinet.example.com/buy/gift/{token}'),
            user=claimant,
            db=db,
        )
        assert res5.status == 'activated'


@pytest.mark.asyncio
async def test_get_gift_purchase_status_empty_token_prefix_returns_404(monkeypatch):
    """Passing empty or prefix-only token like 'GIFT_' returns 404 even if user owns purchases."""
    async with memory_session(monkeypatch, _TABLES) as db:
        user = User(id=10, username='buyer')
        tariff = Tariff(id=1, name='Standard')
        full_token = 'a' * 64
        purchase = GuestPurchase(
            id=1,
            token=full_token,
            contact_type='email',
            contact_value='buyer@example.com',
            tariff_id=1,
            period_days=30,
            amount_kopeks=20000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=10,
        )
        db.add_all([user, tariff, purchase])
        await db.commit()

        # Test GIFT_
        with pytest.raises(HTTPException) as exc_info1:
            await gift_routes.get_gift_purchase_status(token='GIFT_', user=user, db=db)
        assert exc_info1.value.status_code == 404
        assert exc_info1.value.detail == 'Purchase not found'

        # Test GIFT-
        with pytest.raises(HTTPException) as exc_info2:
            await gift_routes.get_gift_purchase_status(token='GIFT-', user=user, db=db)
        assert exc_info2.value.status_code == 404
        assert exc_info2.value.detail == 'Purchase not found'

        # Test empty string / whitespace
        with pytest.raises(HTTPException) as exc_info3:
            await gift_routes.get_gift_purchase_status(token='   ', user=user, db=db)
        assert exc_info3.value.status_code == 404
        assert exc_info3.value.detail == 'Purchase not found'
