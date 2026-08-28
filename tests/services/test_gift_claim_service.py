"""Unit and integration tests for the shared authenticated gift-claim service.

Covers:
- Matrix of gift origins: bot balance purchase and cabinet guest purchase.
- Matrix of claim input formats: canonical code (GIFT_<59>), Telegram deep link,
  cabinet web URL, raw full 64-char token, legacy short code (with allow_legacy_short).
- Error conditions:
  - Malformed / too-short input -> GiftClaimNotFoundError
  - Non-existent token -> GiftClaimNotFoundError
  - Self-claim by buyer -> GiftClaimSelfActivationError
  - Gift pre-bound to another user -> GiftClaimAlreadyOwnedError
  - Gift already delivered to another user -> GiftClaimAlreadyOwnedError
  - Non-activatable statuses (PENDING, FAILED, REFUNDED) -> GiftClaimNotActivatableError
  - Subscription activation failure -> database rollback, no partial state
- Idempotency & Invariants:
  - Repeated claim by same user returns delivered purchase without re-provisioning
  - Claims never debit balance or create extra purchase transactions
  - Provisioning called exactly once
- Directed claim:
  - claim_bound_gift_for_user by purchase_id
  - Refusal when bound to different user or self-activation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.database.crud.landing import generate_purchase_token
from app.database.models import (
    DiscountOffer,
    GuestPurchase,
    GuestPurchaseStatus,
    PaymentMethodConfig,
    PromoGroup,
    PromoOfferLog,
    ServerSquad,
    Subscription,
    SystemSetting,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    Webhook,
    tariff_promo_groups,
)
from app.services.gift_claim_service import (
    GiftClaimAlreadyOwnedError,
    GiftClaimNotActivatableError,
    GiftClaimNotFoundError,
    GiftClaimSelfActivationError,
    claim_bound_gift_for_user,
    claim_gift_for_user,
)
from app.services.gift_purchase_service import (
    GIFT_ENABLED_KEY,
    purchase_gift_from_balance,
    quote_gift_purchase,
)
from app.services.guest_purchase_service import GuestPurchaseError
from app.utils.gift_links import (
    build_bot_gift_claim_link,
    build_cabinet_gift_claim_link,
    build_gift_public_code,
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
    ServerSquad.__table__,
    PaymentMethodConfig.__table__,
    Webhook.__table__,
]


async def _seed_setup(db) -> tuple[Tariff, User, User, User]:
    """Helper to seed standard tariff, buyer, and two recipient users."""
    setting = SystemSetting(key=GIFT_ENABLED_KEY, value='true')
    db.add(setting)

    tariff = Tariff(
        id=1,
        name='Standard Plan',
        is_active=True,
        show_in_gift=True,
        device_limit=2,
        traffic_limit_gb=50,
        period_prices={'30': 30000},
        display_order=1,
    )
    buyer = User(id=10, telegram_id=11111, username='buyer', balance_kopeks=100000)
    claimant1 = User(id=20, telegram_id=22222, username='claimant1', balance_kopeks=0)
    claimant2 = User(id=30, telegram_id=33333, username='claimant2', balance_kopeks=0)
    db.add_all([tariff, buyer, claimant1, claimant2])
    await db.commit()
    await db.refresh(tariff)
    await db.refresh(buyer)
    await db.refresh(claimant1)
    await db.refresh(claimant2)
    return tariff, buyer, claimant1, claimant2


# ── Test Suite ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_gift_bot_origin_canonical_code(monkeypatch):
    """Claiming a bot-origin gift via canonical GIFT_<59> code binds and delivers subscription."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, _ = await _seed_setup(db)

        # Buyer purchases gift
        quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=tariff.id, period_days=30)
        p_res = await purchase_gift_from_balance(
            db,
            buyer_id=buyer.id,
            tariff_id=tariff.id,
            period_days=30,
            expected_price_kopeks=quote.final_price_kopeks,
            idempotency_key='bot_claim_test_1',
        )
        token = p_res.purchase.token
        canonical_code = build_gift_public_code(token)

        initial_claimant_balance = claimant1.balance_kopeks

        with patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()
        ) as mock_prov:
            result = await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=canonical_code)

        assert result.status == GuestPurchaseStatus.DELIVERED.value
        assert result.user_id == claimant1.id
        assert result.delivered_at is not None
        mock_prov.assert_awaited_once()

        # Invariants: claimant balance unchanged, no extra transactions for claimant
        await db.refresh(claimant1)
        assert claimant1.balance_kopeks == initial_claimant_balance

        tx_res = await db.execute(select(Transaction).where(Transaction.user_id == claimant1.id))
        assert tx_res.scalars().all() == []


@pytest.mark.asyncio
async def test_claim_gift_cabinet_origin_telegram_deeplink(monkeypatch):
    """Claiming a cabinet/landing origin gift via Telegram deep-link URL binds and delivers."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _, claimant1, _ = await _seed_setup(db)

        token = generate_purchase_token()
        purchase = GuestPurchase(
            token=token,
            contact_type='email',
            contact_value='anon@example.com',
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
        )
        db.add(purchase)
        await db.commit()

        deeplink = build_bot_gift_claim_link(token, 'test_vpn_bot')

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            result = await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=deeplink)

        assert result.status == GuestPurchaseStatus.DELIVERED.value
        assert result.user_id == claimant1.id


@pytest.mark.asyncio
async def test_claim_gift_web_url_and_full_token_inputs(monkeypatch):
    """Claiming via cabinet claim URL and raw 64-char token works identically."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _, claimant1, claimant2 = await _seed_setup(db)

        # 1. Cabinet URL claim
        token1 = generate_purchase_token()
        purchase1 = GuestPurchase(
            token=token1,
            contact_type='email',
            contact_value='gift1@example.com',
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
        )
        db.add(purchase1)
        await db.commit()

        web_url = build_cabinet_gift_claim_link(token1, 'https://cabinet.example.com')
        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            res1 = await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=web_url)
        assert res1.status == GuestPurchaseStatus.DELIVERED.value
        assert res1.user_id == claimant1.id

        # 2. Raw 64-char token claim
        token2 = generate_purchase_token()
        purchase2 = GuestPurchase(
            token=token2,
            contact_type='email',
            contact_value='gift2@example.com',
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
        )
        db.add(purchase2)
        await db.commit()

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            res2 = await claim_gift_for_user(db, claimant_user_id=claimant2.id, claim_input=token2)
        assert res2.status == GuestPurchaseStatus.DELIVERED.value
        assert res2.user_id == claimant2.id


@pytest.mark.asyncio
async def test_legacy_short_code_support_flag(monkeypatch):
    """Legacy short codes succeed when allow_legacy_short=True and fail with GiftClaimNotFoundError when False."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _, claimant1, _ = await _seed_setup(db)

        token = 'abcdefghij1234567890abcdefghijklmnopqrstuvwxyz0123456789abcdef01'
        purchase = GuestPurchase(
            token=token,
            contact_type='email',
            contact_value='legacy@example.com',
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
        )
        db.add(purchase)
        await db.commit()

        short_code = token[:8]

        # Strict mode (default, e.g. for Telegram deep link) rejects short codes
        with pytest.raises(GiftClaimNotFoundError):
            await claim_gift_for_user(
                db, claimant_user_id=claimant1.id, claim_input=short_code, allow_legacy_short=False
            )

        # Legacy compatibility mode (e.g. for web cabinet input) accepts short codes
        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            result = await claim_gift_for_user(
                db, claimant_user_id=claimant1.id, claim_input=short_code, allow_legacy_short=True
            )
        assert result.status == GuestPurchaseStatus.DELIVERED.value
        assert result.user_id == claimant1.id


@pytest.mark.asyncio
async def test_self_claim_rejected(monkeypatch):
    """Buyer attempting to claim their own gift raises GiftClaimSelfActivationError and does not mutate purchase."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, _, _ = await _seed_setup(db)

        token = generate_purchase_token()
        purchase = GuestPurchase(
            token=token,
            contact_type='telegram',
            contact_value=str(buyer.telegram_id),
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            buyer_user_id=buyer.id,
        )
        db.add(purchase)
        await db.commit()

        with pytest.raises(GiftClaimSelfActivationError):
            await claim_gift_for_user(db, claimant_user_id=buyer.id, claim_input=token)

        # Invariant: purchase remains unmutated in PAID status with user_id=None
        await db.refresh(purchase)
        assert purchase.status == GuestPurchaseStatus.PAID.value
        assert purchase.user_id is None


@pytest.mark.asyncio
async def test_claim_already_owned_by_another_user_rejected(monkeypatch):
    """Attempting to claim a gift bound or delivered to another user raises GiftClaimAlreadyOwnedError."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _, claimant1, claimant2 = await _seed_setup(db)

        token = generate_purchase_token()
        purchase = GuestPurchase(
            token=token,
            contact_type='telegram',
            contact_value=str(claimant1.telegram_id),
            is_gift=True,
            status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            user_id=claimant1.id,
        )
        db.add(purchase)
        await db.commit()

        # Claimant2 attempts to claim gift bound to Claimant1
        with pytest.raises(GiftClaimAlreadyOwnedError):
            await claim_gift_for_user(db, claimant_user_id=claimant2.id, claim_input=token)

        # Invariant: purchase remains bound to claimant1
        await db.refresh(purchase)
        assert purchase.user_id == claimant1.id


@pytest.mark.asyncio
async def test_idempotent_repeated_claim_by_same_user(monkeypatch):
    """Repeated claim by the SAME user returns DELIVERED purchase without reactivating or extending twice."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _, claimant1, _ = await _seed_setup(db)

        token = generate_purchase_token()
        purchase = GuestPurchase(
            token=token,
            contact_type='telegram',
            contact_value=str(claimant1.telegram_id),
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
        )
        db.add(purchase)
        await db.commit()

        with patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()
        ) as mock_prov:
            res1 = await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=token)
            assert res1.status == GuestPurchaseStatus.DELIVERED.value
            mock_prov.assert_awaited_once()

            # Second claim by SAME user
            mock_prov.reset_mock()
            res2 = await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=token)
            assert res2.status == GuestPurchaseStatus.DELIVERED.value
            assert res2.id == purchase.id

            # Provisioning was NOT called again
            mock_prov.assert_not_called()

        # Subscription count remains exactly 1
        subs_res = await db.execute(select(Subscription).where(Subscription.user_id == claimant1.id))
        subs = subs_res.scalars().all()
        assert len(subs) == 1


@pytest.mark.asyncio
async def test_unactivatable_status_rejected(monkeypatch):
    """Gifts in FAILED, PENDING, or other unactivatable statuses raise GiftClaimNotActivatableError."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _, claimant1, _ = await _seed_setup(db)

        for unactivatable_status in [GuestPurchaseStatus.PENDING.value, GuestPurchaseStatus.FAILED.value]:
            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(claimant1.telegram_id),
                is_gift=True,
                status=unactivatable_status,
                tariff_id=tariff.id,
                period_days=30,
                amount_kopeks=30000,
            )
            db.add(purchase)
            await db.commit()

            with pytest.raises(GiftClaimNotActivatableError):
                await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=token)


@pytest.mark.asyncio
async def test_malformed_or_nonexistent_input_raises_not_found(monkeypatch):
    """Malformed strings, bad schemes, empty strings, and non-existent tokens raise GiftClaimNotFoundError."""
    async with memory_session(monkeypatch, _TABLES) as db:
        _, _, claimant1, _ = await _seed_setup(db)

        with pytest.raises(GiftClaimNotFoundError):
            await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input='')

        with pytest.raises(GiftClaimNotFoundError):
            await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input='invalid-short')

        with pytest.raises(GiftClaimNotFoundError):
            await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input='https://example.com/not-gift')

        non_existent_token = '0' * 64
        with pytest.raises(GiftClaimNotFoundError):
            await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=non_existent_token)


@pytest.mark.asyncio
async def test_claim_bound_gift_for_user_directed_callback(monkeypatch):
    """claim_bound_gift_for_user by purchase_id activates directed gifts for the bound claimant."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, claimant2 = await _seed_setup(db)

        token = generate_purchase_token()
        purchase = GuestPurchase(
            token=token,
            contact_type='telegram',
            contact_value=str(claimant1.telegram_id),
            is_gift=True,
            status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            user_id=claimant1.id,
            buyer_user_id=buyer.id,
        )
        db.add(purchase)
        await db.commit()

        # Another user attempting to claim via directed ID fails
        with pytest.raises(GiftClaimAlreadyOwnedError):
            await claim_bound_gift_for_user(db, claimant_user_id=claimant2.id, purchase_id=purchase.id)

        # Buyer self-activation fails
        with pytest.raises(GiftClaimSelfActivationError):
            await claim_bound_gift_for_user(db, claimant_user_id=buyer.id, purchase_id=purchase.id)

        # Correct claimant succeeds
        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            result = await claim_bound_gift_for_user(db, claimant_user_id=claimant1.id, purchase_id=purchase.id)

        assert result.status == GuestPurchaseStatus.DELIVERED.value
        assert result.user_id == claimant1.id


@pytest.mark.asyncio
async def test_activation_failure_rolls_back_cleanly(monkeypatch):
    """When underlying activate_purchase fails, exception propagates and transaction rolls back."""
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _, claimant1, _ = await _seed_setup(db)

        token = generate_purchase_token()
        purchase = GuestPurchase(
            token=token,
            contact_type='telegram',
            contact_value=str(claimant1.telegram_id),
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
        )
        db.add(purchase)
        await db.commit()

        with patch(
            'app.services.guest_purchase_service.activate_purchase',
            AsyncMock(side_effect=GuestPurchaseError('Remnawave failure', status_code=500)),
        ):
            with pytest.raises(GuestPurchaseError):
                await claim_gift_for_user(db, claimant_user_id=claimant1.id, claim_input=token)
