"""Unit and integration tests for the shared gift purchase domain service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.database.models import (
    DiscountOffer,
    GuestPurchase,
    GuestPurchaseStatus,
    PaymentMethod,
    PromoGroup,
    PromoOfferLog,
    Subscription,
    SystemSetting,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from app.services.gift_purchase_service import (
    GIFT_ENABLED_KEY,
    GiftFeatureDisabledError,
    GiftIdempotencyConflictError,
    GiftInsufficientBalanceError,
    GiftPeriodUnavailableError,
    GiftPriceChangedError,
    GiftPurchaseRestrictedError,
    GiftPurchaseResult,
    GiftQuote,
    GiftRecipient,
    GiftTariffUnavailableError,
    is_gift_enabled,
    list_gift_offers,
    purchase_gift_from_balance,
    quote_gift_purchase,
)
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
]


# ── Step 1: Feature switch, Catalog, and Quotes ─────────────────────────────


@pytest.mark.asyncio
async def test_is_gift_enabled_reflects_cabinet_gift_setting(monkeypatch):
    """Missing or non-true CABINET_GIFT_ENABLED disables the service."""
    async with memory_session(monkeypatch, _TABLES) as db:
        # Default: absent setting -> disabled
        assert await is_gift_enabled(db) is False

        # Explicitly false
        setting = SystemSetting(key=GIFT_ENABLED_KEY, value='false')
        db.add(setting)
        await db.commit()
        assert await is_gift_enabled(db) is False

        # Explicitly true (case-insensitive)
        setting.value = 'True'
        await db.commit()
        assert await is_gift_enabled(db) is True


@pytest.mark.asyncio
async def test_list_gift_offers_returns_empty_when_feature_disabled(monkeypatch):
    """When CABINET_GIFT_ENABLED is missing or false, catalog is empty."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff = Tariff(
            name='Test Tariff',
            is_active=True,
            show_in_gift=True,
            device_limit=2,
            traffic_limit_gb=50,
            period_prices={'30': 30000},
            display_order=1,
        )
        db.add(tariff)
        await db.commit()

        offers = await list_gift_offers(db)
        assert offers == []


@pytest.mark.asyncio
async def test_list_gift_offers_filters_and_orders_tariffs(monkeypatch):
    """Catalog only contains active, show_in_gift tariffs with prices, ordered by display_order then id."""
    async with memory_session(monkeypatch, _TABLES) as db:
        # Enable feature
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        # Tariff 1: active, show_in_gift, display_order 2
        t1 = Tariff(
            name='Second Tariff',
            is_active=True,
            show_in_gift=True,
            device_limit=1,
            traffic_limit_gb=100,
            period_prices={'30': 30000, '90': 80000},
            display_order=2,
        )
        # Tariff 2: active, show_in_gift, display_order 1
        t2 = Tariff(
            name='First Tariff',
            is_active=True,
            show_in_gift=True,
            device_limit=3,
            traffic_limit_gb=0,  # unlimited
            period_prices={'30': 50000},
            display_order=1,
        )
        # Tariff 3: inactive, show_in_gift
        t3 = Tariff(
            name='Inactive Tariff',
            is_active=False,
            show_in_gift=True,
            period_prices={'30': 10000},
            display_order=0,
        )
        # Tariff 4: active, NOT show_in_gift
        t4 = Tariff(
            name='Hidden Tariff',
            is_active=True,
            show_in_gift=False,
            period_prices={'30': 20000},
            display_order=0,
        )
        # Tariff 5: active, show_in_gift, but empty period prices
        t5 = Tariff(
            name='No Price Tariff',
            is_active=True,
            show_in_gift=True,
            period_prices={},
            display_order=3,
        )
        db.add_all([t1, t2, t3, t4, t5])
        await db.commit()

        offers = await list_gift_offers(db)
        assert len(offers) == 2
        assert offers[0].tariff_id == t2.id
        assert offers[0].tariff_name == 'First Tariff'
        assert offers[0].device_limit == 3
        assert offers[0].traffic_limit_gb == 0
        assert len(offers[0].quotes) == 1
        assert offers[0].quotes[0].period_days == 30
        assert offers[0].quotes[0].final_price_kopeks == 50000

        assert offers[1].tariff_id == t1.id
        assert offers[1].tariff_name == 'Second Tariff'
        assert len(offers[1].quotes) == 2
        assert [q.period_days for q in offers[1].quotes] == [30, 90]


@pytest.mark.asyncio
async def test_list_gift_offers_applies_sender_discounts_and_clamps(monkeypatch):
    """Sender promo-group and promo-offer discounts are applied in quotes and clamped to >= 1 kopek."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        promo_group = PromoGroup(
            name='VIP',
            period_discounts={'30': 20},
            is_default=False,
        )
        db.add(promo_group)
        await db.flush()

        buyer = User(
            telegram_id=123456,
            balance_kopeks=50000,
            promo_group_id=promo_group.id,
            promo_offer_discount_percent=50,  # 50% personal promo offer
        )
        tariff = Tariff(
            name='VIP Tariff',
            is_active=True,
            show_in_gift=True,
            device_limit=2,
            traffic_limit_gb=50,
            period_prices={'30': 10000},  # 100.00 RUB
            display_order=1,
        )
        db.add_all([buyer, tariff])
        await db.commit()

        offers = await list_gift_offers(db, buyer=buyer)
        assert len(offers) == 1
        quote = offers[0].quotes[0]
        # 10000 - 20% (group) = 8000; 8000 - 50% (offer) = 4000
        assert quote.original_price_kopeks == 10000
        assert quote.promo_group_discount_kopeks == 2000
        assert quote.promo_offer_discount_kopeks == 4000
        assert quote.final_price_kopeks == 4000
        assert quote.consumes_promo_offer is True


@pytest.mark.asyncio
async def test_quote_gift_purchase_success_and_errors(monkeypatch):
    """quote_gift_purchase returns typed GiftQuote on valid selection and raises typed errors on invalid ones."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        tariff = Tariff(
            name='Base Tariff',
            is_active=True,
            show_in_gift=True,
            device_limit=2,
            traffic_limit_gb=50,
            period_prices={'30': 30000, '90': 80000},
        )
        hidden_tariff = Tariff(
            name='Hidden Tariff',
            is_active=True,
            show_in_gift=False,
            period_prices={'30': 30000},
        )
        db.add_all([tariff, hidden_tariff])
        await db.commit()

        # Success quote
        quote = await quote_gift_purchase(db, None, tariff.id, 30)
        assert isinstance(quote, GiftQuote)
        assert quote.tariff_id == tariff.id
        assert quote.tariff_name == 'Base Tariff'
        assert quote.period_days == 30
        assert quote.final_price_kopeks == 30000
        assert quote.consumes_promo_offer is False

        # Missing tariff
        with pytest.raises(GiftTariffUnavailableError):
            await quote_gift_purchase(db, None, 99999, 30)

        # Hidden tariff
        with pytest.raises(GiftTariffUnavailableError):
            await quote_gift_purchase(db, None, hidden_tariff.id, 30)

        # Unpriced period
        with pytest.raises(GiftPeriodUnavailableError):
            await quote_gift_purchase(db, None, tariff.id, 365)

        # Disabled feature
        setting_res = await db.execute(select(SystemSetting).where(SystemSetting.key == GIFT_ENABLED_KEY))
        setting = setting_res.scalar_one()
        setting.value = 'false'
        await db.commit()

        with pytest.raises(GiftFeatureDisabledError):
            await quote_gift_purchase(db, None, tariff.id, 30)


# ── Step 2: Financial Transactions, Failures, and Idempotency ────────────────


@pytest.mark.asyncio
async def test_purchase_gift_from_balance_success_bot_mode(monkeypatch):
    """Successful balance purchase: exact debit, GIFT_PAYMENT transaction, promo offer consumed, paid purchase created."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        buyer = User(
            telegram_id=111222,
            username='gift_sender',
            balance_kopeks=50000,
            promo_offer_discount_percent=20,
        )
        tariff = Tariff(
            name='Premium Gift',
            is_active=True,
            show_in_gift=True,
            device_limit=5,
            traffic_limit_gb=200,
            period_prices={'30': 30000},
        )
        db.add_all([buyer, tariff])
        await db.commit()

        # Base price 30000 - 20% = 24000 kopeks
        expected_price = 24000
        idempotency_key = 'checkout-key-bot-001'

        with patch('app.services.gift_purchase_service.emit_transaction_side_effects', AsyncMock()) as emit_mock:
            result = await purchase_gift_from_balance(
                db=db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=expected_price,
                idempotency_key=idempotency_key,
                source='bot',
            )

        assert isinstance(result, GiftPurchaseResult)
        assert result.is_idempotent_replay is False
        assert result.remaining_balance_kopeks == 26000  # 50000 - 24000
        assert result.quote.final_price_kopeks == 24000

        # Verify GuestPurchase fields
        purchase = result.purchase
        assert purchase.is_gift is True
        assert purchase.source == 'bot'
        assert purchase.buyer_user_id == buyer.id
        assert purchase.tariff_id == tariff.id
        assert purchase.period_days == 30
        assert purchase.amount_kopeks == 24000
        assert purchase.status == GuestPurchaseStatus.PAID.value
        assert purchase.paid_at is not None
        assert purchase.gift_recipient_type is None
        assert purchase.gift_recipient_value is None
        assert purchase.gift_message is None
        assert purchase.idempotency_key == idempotency_key
        assert purchase.contact_type == 'telegram'
        assert purchase.contact_value == '@gift_sender'

        # Verify Transaction fields
        tx = result.transaction
        assert tx.user_id == buyer.id
        assert tx.type == TransactionType.GIFT_PAYMENT.value
        assert tx.amount_kopeks == -24000  # Negative debit
        assert tx.payment_method == PaymentMethod.BALANCE.value
        assert tx.is_completed is True
        assert tx.external_id == f'gift_bot_{idempotency_key}'
        assert tx.description == 'Gift: Premium Gift (30d)'

        # Verify buyer balance and promo offer consumption
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 26000
        assert buyer.promo_offer_discount_percent == 0

        # Verify side effects were emitted
        emit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_purchase_gift_from_balance_cabinet_recipient(monkeypatch):
    """Cabinet mode with GiftRecipient persists recipient fields and custom transaction description."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        buyer = User(
            email='sender@example.com',
            balance_kopeks=40000,
        )
        tariff = Tariff(
            name='Cabinet Tariff',
            is_active=True,
            show_in_gift=True,
            device_limit=1,
            traffic_limit_gb=50,
            period_prices={'30': 30000},
        )
        db.add_all([buyer, tariff])
        await db.commit()

        recipient = GiftRecipient(
            recipient_type='telegram',
            recipient_value='@best_friend',
            gift_message='Happy Birthday!',
        )
        idempotency_key = 'checkout-key-cabinet-001'

        result = await purchase_gift_from_balance(
            db=db,
            buyer_id=buyer.id,
            tariff_id=tariff.id,
            period_days=30,
            expected_price_kopeks=30000,
            idempotency_key=idempotency_key,
            source='cabinet',
            recipient=recipient,
        )

        assert result.purchase.source == 'cabinet'
        assert result.purchase.gift_recipient_type == 'telegram'
        assert result.purchase.gift_recipient_value == '@best_friend'
        assert result.purchase.gift_message == 'Happy Birthday!'
        assert result.purchase.contact_type == 'email'
        assert result.purchase.contact_value == 'sender@example.com'
        assert result.transaction.description == 'Gift: Cabinet Tariff (30d) -> @best_friend'


@pytest.mark.asyncio
async def test_purchase_gift_insufficient_balance_rolls_back(monkeypatch):
    """Insufficient balance raises typed error and leaves DB untouched."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        buyer = User(
            telegram_id=999,
            balance_kopeks=15000,
            promo_offer_discount_percent=10,
        )
        tariff = Tariff(
            name='Tariff A',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([buyer, tariff])
        await db.commit()

        with pytest.raises(GiftInsufficientBalanceError) as exc_info:
            await purchase_gift_from_balance(
                db=db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=27000,  # 30000 - 10% = 27000
                idempotency_key='fail-key-balance',
            )

        assert exc_info.value.required_kopeks == 27000
        assert exc_info.value.available_kopeks == 15000

        # State preserved
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 15000
        assert buyer.promo_offer_discount_percent == 10
        purchases = (await db.execute(select(GuestPurchase))).scalars().all()
        assert purchases == []
        transactions = (await db.execute(select(Transaction))).scalars().all()
        assert transactions == []


@pytest.mark.asyncio
async def test_purchase_gift_restricted_user(monkeypatch):
    """User with restriction_subscription cannot buy gifts."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        buyer = User(
            telegram_id=888,
            balance_kopeks=50000,
            restriction_subscription=True,
        )
        tariff = Tariff(
            name='Tariff A',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([buyer, tariff])
        await db.commit()

        with pytest.raises(GiftPurchaseRestrictedError):
            await purchase_gift_from_balance(
                db=db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=30000,
                idempotency_key='fail-key-restricted',
            )

        await db.refresh(buyer)
        assert buyer.balance_kopeks == 50000


@pytest.mark.asyncio
async def test_purchase_gift_stale_expected_price_rejected(monkeypatch):
    """If expected price does not match fresh price calculation, fail without debit."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        buyer = User(
            telegram_id=777,
            balance_kopeks=50000,
        )
        tariff = Tariff(
            name='Tariff A',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        db.add_all([buyer, tariff])
        await db.commit()

        with pytest.raises(GiftPriceChangedError) as exc_info:
            await purchase_gift_from_balance(
                db=db,
                buyer_id=buyer.id,
                tariff_id=tariff.id,
                period_days=30,
                expected_price_kopeks=25000,  # Wrong expected price (real is 30000)
                idempotency_key='fail-key-price',
            )

        assert exc_info.value.expected_price_kopeks == 25000
        assert exc_info.value.fresh_quote.final_price_kopeks == 30000

        await db.refresh(buyer)
        assert buyer.balance_kopeks == 50000


@pytest.mark.asyncio
async def test_idempotent_replay_and_conflict(monkeypatch):
    """Repeating same idempotency key returns original result; changing input raises conflict."""
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add(SystemSetting(key=GIFT_ENABLED_KEY, value='true'))

        buyer = User(
            telegram_id=555,
            balance_kopeks=100000,
        )
        tariff1 = Tariff(
            name='Tariff 1',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 30000},
        )
        tariff2 = Tariff(
            name='Tariff 2',
            is_active=True,
            show_in_gift=True,
            period_prices={'30': 40000},
        )
        db.add_all([buyer, tariff1, tariff2])
        await db.commit()

        idempotency_key = 'stable-checkout-id-123'

        # First call: initial purchase
        res1 = await purchase_gift_from_balance(
            db=db,
            buyer_id=buyer.id,
            tariff_id=tariff1.id,
            period_days=30,
            expected_price_kopeks=30000,
            idempotency_key=idempotency_key,
        )
        assert res1.is_idempotent_replay is False
        assert res1.remaining_balance_kopeks == 70000

        # Second call: exact same input -> idempotent replay
        res2 = await purchase_gift_from_balance(
            db=db,
            buyer_id=buyer.id,
            tariff_id=tariff1.id,
            period_days=30,
            expected_price_kopeks=30000,
            idempotency_key=idempotency_key,
        )
        assert res2.is_idempotent_replay is True
        assert res2.purchase.id == res1.purchase.id
        assert res2.transaction.id == res1.transaction.id
        assert res2.remaining_balance_kopeks == 70000  # No second debit

        # Verify only 1 purchase and 1 transaction exist in DB
        purchases = (await db.execute(select(GuestPurchase))).scalars().all()
        assert len(purchases) == 1
        transactions = (await db.execute(select(Transaction))).scalars().all()
        assert len(transactions) == 1

        # Third call: same idempotency key with different tariff -> conflict error
        with pytest.raises(GiftIdempotencyConflictError):
            await purchase_gift_from_balance(
                db=db,
                buyer_id=buyer.id,
                tariff_id=tariff2.id,
                period_days=30,
                expected_price_kopeks=40000,
                idempotency_key=idempotency_key,
            )

        # Fourth call: different idempotency key -> creates second gift
        res3 = await purchase_gift_from_balance(
            db=db,
            buyer_id=buyer.id,
            tariff_id=tariff2.id,
            period_days=30,
            expected_price_kopeks=40000,
            idempotency_key='different-checkout-id-456',
        )
        assert res3.is_idempotent_replay is False
        assert res3.remaining_balance_kopeks == 30000  # 70000 - 40000
        purchases_after = (await db.execute(select(GuestPurchase))).scalars().all()
        assert len(purchases_after) == 2


@pytest.mark.asyncio
async def test_replay_never_attaches_another_gifts_transaction(monkeypatch):
    """Повтор обязан вернуть списание ИМЕННО этого подарка либо ничего.

    Прежний фоллбек брал последнюю GIFT_PAYMENT покупателя с order_by(id.desc()):
    у того, кто дарил дважды, повтор показывал сумму от другого подарка.
    """
    from app.database.models import GuestPurchase, GuestPurchaseStatus, PaymentMethod, Transaction, TransactionType
    from app.services.gift_purchase_service import _build_gift_transaction_external_id, _find_gift_transaction
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, [User.__table__, GuestPurchase.__table__, Transaction.__table__]) as db:
        earlier = Transaction(
            user_id=1,
            type=TransactionType.GIFT_PAYMENT.value,
            amount_kopeks=50000,
            description='другой подарок',
            payment_method=PaymentMethod.BALANCE.value,
            external_id=_build_gift_transaction_external_id('bot', 'checkout-other'),
            is_completed=True,
        )
        db.add(earlier)
        await db.commit()

        purchase = GuestPurchase(
            token='t' * 64,
            contact_type='telegram',
            contact_value='@buyer',
            period_days=30,
            amount_kopeks=10000,
            status=GuestPurchaseStatus.PAID.value,
            is_gift=True,
            source='cabinet',
            buyer_user_id=1,
            idempotency_key='checkout-mine',
        )
        db.add(purchase)
        await db.commit()

        # Своей транзакции ещё нет — вернуть чужую нельзя
        assert await _find_gift_transaction(db, purchase, 'checkout-mine', 'bot') is None

        mine = Transaction(
            user_id=1,
            type=TransactionType.GIFT_PAYMENT.value,
            amount_kopeks=10000,
            description='этот подарок',
            payment_method=PaymentMethod.BALANCE.value,
            # Ключ собран из source покупки (cabinet), а повтор придёт из бота
            external_id=_build_gift_transaction_external_id('cabinet', 'checkout-mine'),
            is_completed=True,
        )
        db.add(mine)
        await db.commit()

        found = await _find_gift_transaction(db, purchase, 'checkout-mine', 'bot')

    assert found is not None
    assert found.external_id == mine.external_id
    assert found.amount_kopeks == 10000
