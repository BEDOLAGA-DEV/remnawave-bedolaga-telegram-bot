"""Unit tests for the source-neutral sender gift history domain service."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    DiscountOffer,
    GuestPurchase,
    GuestPurchaseStatus,
    PromoGroup,
    PromoOfferLog,
    Subscription,
    SystemSetting,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from app.services.gift_history_service import (
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    MIN_HISTORY_LIMIT,
    GiftHistoryItem,
    format_safe_recipient,
    get_sender_gift,
    has_sender_gifts,
    list_sender_gifts,
)
from app.utils.gift_links import build_gift_claim_artifacts, build_gift_public_code
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

# 64-character tokens for test purchases
TOKEN_1 = 'a' * 64
TOKEN_2 = 'b' * 64
TOKEN_3 = 'c' * 64
TOKEN_4 = 'd' * 64
TOKEN_5 = 'e' * 64
TOKEN_6 = 'f' * 64
TOKEN_7 = 'g' * 64
TOKEN_8 = 'h' * 64


async def _create_test_user(
    db: AsyncSession,
    telegram_id: int | None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
) -> User:
    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        email=email,
    )
    db.add(user)
    await db.flush()
    return user


async def _create_test_tariff(
    db: AsyncSession,
    name: str = 'Premium VIP',
    traffic_limit_gb: int | None = 100,
    device_limit: int = 3,
) -> Tariff:
    tariff = Tariff(
        name=name,
        is_active=True,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        period_prices={'30': 30000},
    )
    db.add(tariff)
    await db.flush()
    return tariff


# ── Tests for Gift History Service ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sender_gifts_source_neutral_and_status_filtering(monkeypatch):
    """History returns bot-, cabinet-, and landing-origin gifts for the buyer,
    filtering ONLY to PAID, PENDING_ACTIVATION, and DELIVERED statuses.
    """
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001, username='buyer_one')
        other_buyer = await _create_test_user(db, telegram_id=1002, username='buyer_two')
        recipient = await _create_test_user(db, telegram_id=2001, username='gift_recipient')
        tariff = await _create_test_tariff(db, name='Standard 30d')

        now = datetime.now(UTC)

        # 1. Eligible bot gift (PAID)
        p1 = GuestPurchase(
            token=TOKEN_1,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_one',
            created_at=now - timedelta(minutes=10),
            paid_at=now - timedelta(minutes=9),
        )
        # 2. Eligible cabinet gift (PENDING_ACTIVATION)
        p2 = GuestPurchase(
            token=TOKEN_2,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='cabinet',
            status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_one',
            created_at=now - timedelta(minutes=8),
            paid_at=now - timedelta(minutes=7),
        )
        # 3. Eligible landing gift (DELIVERED, claimed by recipient)
        p3 = GuestPurchase(
            token=TOKEN_3,
            buyer_user_id=buyer.id,
            user_id=recipient.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='landing',
            status=GuestPurchaseStatus.DELIVERED.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_one',
            created_at=now - timedelta(minutes=6),
            paid_at=now - timedelta(minutes=5),
            delivered_at=now - timedelta(minutes=1),
        )
        # 4. Ineligible: PENDING status
        p4 = GuestPurchase(
            token=TOKEN_4,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PENDING.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_one',
            created_at=now - timedelta(minutes=4),
        )
        # 5. Ineligible: FAILED status
        p5 = GuestPurchase(
            token=TOKEN_5,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='cabinet',
            status=GuestPurchaseStatus.FAILED.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_one',
            created_at=now - timedelta(minutes=3),
        )
        # 6. Ineligible: EXPIRED status
        p6 = GuestPurchase(
            token=TOKEN_6,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.EXPIRED.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_one',
            created_at=now - timedelta(minutes=2),
        )
        # 7. Ineligible: Not a gift (is_gift=False)
        p7 = GuestPurchase(
            token=TOKEN_7,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=False,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_one',
            created_at=now - timedelta(minutes=1),
        )
        # 8. Ineligible: Belongs to another buyer
        p8 = GuestPurchase(
            token=TOKEN_8,
            buyer_user_id=other_buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer_two',
            created_at=now,
        )

        db.add_all([p1, p2, p3, p4, p5, p6, p7, p8])
        await db.commit()

        items, total_count = await list_sender_gifts(db, buyer_id=buyer.id)

        assert total_count == 3
        assert len(items) == 3
        returned_tokens = [item.token for item in items]
        assert TOKEN_3 in returned_tokens
        assert TOKEN_2 in returned_tokens
        assert TOKEN_1 in returned_tokens
        assert TOKEN_4 not in returned_tokens
        assert TOKEN_5 not in returned_tokens
        assert TOKEN_6 not in returned_tokens
        assert TOKEN_7 not in returned_tokens
        assert TOKEN_8 not in returned_tokens


@pytest.mark.asyncio
async def test_list_sender_gifts_stable_ordering_with_equal_timestamps(monkeypatch):
    """Ordering must be strictly `created_at DESC, id DESC` for deterministic pagination."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        tariff = await _create_test_tariff(db)

        base_time = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)

        # Three purchases with the EXACT SAME created_at timestamp
        p1 = GuestPurchase(
            token='1' * 64,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=base_time,
        )
        p2 = GuestPurchase(
            token='2' * 64,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='cabinet',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=base_time,
        )
        # One purchase with a newer timestamp
        p3 = GuestPurchase(
            token='3' * 64,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=base_time + timedelta(hours=1),
        )
        # One purchase with an older timestamp
        p0 = GuestPurchase(
            token='0' * 64,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=base_time - timedelta(hours=1),
        )

        db.add_all([p1, p2, p3, p0])
        await db.commit()

        items, total_count = await list_sender_gifts(db, buyer_id=buyer.id, limit=10)

        assert total_count == 4
        # Expected order: p3 (newest time), then p2 (same time, higher id), then p1 (same time, lower id), then p0 (oldest)
        assert items[0].token == p3.token
        assert items[1].token == p2.token
        assert items[2].token == p1.token
        assert items[3].token == p0.token


@pytest.mark.asyncio
async def test_list_sender_gifts_pagination_and_boundary_clamping(monkeypatch):
    """Pagination metadata must be accurate and limit/offset parameters clamped."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        tariff = await _create_test_tariff(db)
        now = datetime.now(UTC)

        # Create 12 gift purchases
        purchases = []
        for i in range(12):
            purchases.append(
                GuestPurchase(
                    token=f'{i:02d}' * 32,
                    buyer_user_id=buyer.id,
                    tariff_id=tariff.id,
                    is_gift=True,
                    source='bot',
                    status=GuestPurchaseStatus.PAID.value,
                    period_days=30,
                    amount_kopeks=30000,
                    contact_type='telegram',
                    contact_value='@buyer',
                    created_at=now + timedelta(seconds=i),
                )
            )
        db.add_all(purchases)
        await db.commit()

        # Page 1: 5 items
        page1, count1 = await list_sender_gifts(db, buyer_id=buyer.id, offset=0, limit=5)
        assert count1 == 12
        assert len(page1) == 5
        assert page1[0].token == purchases[11].token  # newest first
        assert page1[4].token == purchases[7].token

        # Page 2: 5 items
        page2, count2 = await list_sender_gifts(db, buyer_id=buyer.id, offset=5, limit=5)
        assert count2 == 12
        assert len(page2) == 5
        assert page2[0].token == purchases[6].token
        assert page2[4].token == purchases[2].token

        # Page 3: 2 remaining items
        page3, count3 = await list_sender_gifts(db, buyer_id=buyer.id, offset=10, limit=5)
        assert count3 == 12
        assert len(page3) == 2
        assert page3[0].token == purchases[1].token
        assert page3[1].token == purchases[0].token

        # Page 4: beyond total count
        page4, count4 = await list_sender_gifts(db, buyer_id=buyer.id, offset=15, limit=5)
        assert count4 == 12
        assert len(page4) == 0

        # Boundary clamping: limit <= 0 clamped to MIN_HISTORY_LIMIT (1)
        clamped_low, _ = await list_sender_gifts(db, buyer_id=buyer.id, offset=0, limit=-5)
        assert len(clamped_low) == MIN_HISTORY_LIMIT

        # Boundary clamping: limit > MAX_HISTORY_LIMIT (500) clamped to MAX_HISTORY_LIMIT
        clamped_high, _ = await list_sender_gifts(db, buyer_id=buyer.id, offset=0, limit=999)
        assert len(clamped_high) == 12  # returns all 12 without error
        assert MAX_HISTORY_LIMIT == 500
        assert DEFAULT_HISTORY_LIMIT == 10

        # Negative offset clamped to 0
        clamped_offset, _ = await list_sender_gifts(db, buyer_id=buyer.id, offset=-10, limit=5)
        assert len(clamped_offset) == 5
        assert clamped_offset[0].token == purchases[11].token


@pytest.mark.asyncio
async def test_list_sender_gifts_returns_more_than_legacy_fifty_item_cap(monkeypatch):
    """The cabinet's 100-item request must not be silently truncated to the old 50-item cap."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        tariff = await _create_test_tariff(db)
        now = datetime.now(UTC)

        purchases = [
            GuestPurchase(
                token=f'{i:064d}',
                buyer_user_id=buyer.id,
                tariff_id=tariff.id,
                is_gift=True,
                source='cabinet',
                status=GuestPurchaseStatus.PAID.value,
                period_days=30,
                amount_kopeks=30000,
                contact_type='telegram',
                contact_value='@buyer',
                created_at=now + timedelta(seconds=i),
            )
            for i in range(120)
        ]
        db.add_all(purchases)
        await db.commit()

        items, total_count = await list_sender_gifts(db, buyer_id=buyer.id, limit=100)

        assert total_count == 120
        assert len(items) == 100
        assert items[0].token == purchases[-1].token


@pytest.mark.asyncio
async def test_list_sender_gifts_graceful_on_missing_deleted_tariff(monkeypatch):
    """If a tariff was deleted (ON DELETE SET NULL), the history item must load gracefully."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        p = GuestPurchase(
            token=TOKEN_1,
            buyer_user_id=buyer.id,
            tariff_id=None,  # missing / deleted tariff
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=60,
            amount_kopeks=60000,
            contact_type='telegram',
            contact_value='@buyer',
        )
        db.add(p)
        await db.commit()

        items, total_count = await list_sender_gifts(db, buyer_id=buyer.id)

        assert total_count == 1
        item = items[0]
        assert isinstance(item, GiftHistoryItem)
        assert item.tariff_id is None
        assert item.tariff_name is None
        assert item.period_days == 60
        assert item.device_limit == 1
        assert item.traffic_limit_gb is None
        assert item.status == GuestPurchaseStatus.PAID.value


@pytest.mark.asyncio
async def test_safe_recipient_display_formatting(monkeypatch):
    """Safe recipient display formats username or masked email without leaking private data (names, IDs)."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        tariff = await _create_test_tariff(db)
        now = datetime.now(UTC)

        # 1. Delivered to user with @username
        user_with_username = await _create_test_user(db, telegram_id=2001, username='cool_user')
        p1 = GuestPurchase(
            token=TOKEN_1,
            buyer_user_id=buyer.id,
            user_id=user_with_username.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.DELIVERED.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=now,
            delivered_at=now,
        )

        # 2. Delivered to user with name only (no username, no email) -> must NOT reveal name!
        user_with_name = await _create_test_user(
            db, telegram_id=2002, first_name='Ivan', last_name='Petrov', username=None
        )
        p2 = GuestPurchase(
            token=TOKEN_2,
            buyer_user_id=buyer.id,
            user_id=user_with_name.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.DELIVERED.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=now,
            delivered_at=now,
        )

        # 3. Delivered to email-only user -> masked email
        user_with_email = await _create_test_user(db, telegram_id=None, email='supersecret@domain.com', username=None)
        p3 = GuestPurchase(
            token=TOKEN_3,
            buyer_user_id=buyer.id,
            user_id=user_with_email.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.DELIVERED.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=now,
            delivered_at=now,
        )

        # 4. Unclaimed gift with directed recipient contact
        p4 = GuestPurchase(
            token=TOKEN_4,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            gift_recipient_type='telegram',
            gift_recipient_value='@target_friend',
            created_at=now,
        )

        # 5. Unclaimed code-only gift (no recipient value)
        p5 = GuestPurchase(
            token=TOKEN_5,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=now,
        )

        # 6. Delivered to user with only telegram_id (no username, no email) -> must NOT reveal ID!
        user_id_only = await _create_test_user(db, telegram_id=987654321)
        p6 = GuestPurchase(
            token=TOKEN_6,
            buyer_user_id=buyer.id,
            user_id=user_id_only.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.DELIVERED.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
            created_at=now,
            delivered_at=now,
        )

        db.add_all([p1, p2, p3, p4, p5, p6])
        await db.commit()

        item1 = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p1.id)
        assert item1 is not None
        assert item1.recipient_display == '@cool_user'

        item2 = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p2.id)
        assert item2 is not None
        assert item2.recipient_display is None  # No leak of Ivan Petrov!

        item3 = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p3.id)
        assert item3 is not None
        assert item3.recipient_display == 'su***@domain.com'
        assert 'supersecret' not in item3.recipient_display

        item4 = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p4.id)
        assert item4 is not None
        assert item4.recipient_display == '@target_friend'

        item5 = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p5.id)
        assert item5 is not None
        assert item5.recipient_display is None

        item6 = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p6.id)
        assert item6 is not None
        assert item6.recipient_display is None  # No leak of Telegram ID!

        # Direct helper check
        assert format_safe_recipient(None, None) is None
        assert format_safe_recipient(user_with_username) == '@cool_user'
        assert format_safe_recipient(user_with_name) is None
        assert format_safe_recipient(user_id_only) is None


@pytest.mark.asyncio
async def test_get_sender_gift_exact_owner_and_status_checks(monkeypatch):
    """get_sender_gift returns the item only if buyer_id matches and status is eligible."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        other_buyer = await _create_test_user(db, telegram_id=1002)
        tariff = await _create_test_tariff(db)

        p_valid = GuestPurchase(
            token=TOKEN_1,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
        )
        p_pending = GuestPurchase(
            token=TOKEN_2,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PENDING.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
        )
        p_other = GuestPurchase(
            token=TOKEN_3,
            buyer_user_id=other_buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@other',
        )

        db.add_all([p_valid, p_pending, p_other])
        await db.commit()

        # 1. Valid lookup
        item = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p_valid.id)
        assert item is not None
        assert item.purchase_id == p_valid.id
        assert item.token == TOKEN_1
        assert item.public_code == build_gift_public_code(TOKEN_1)
        assert item.tariff_name == 'Premium VIP'
        assert item.device_limit == 3
        assert item.traffic_limit_gb == 100
        assert item.is_claimable is True
        assert item.is_delivered is False

        # 2. Ineligible status (PENDING) -> returns None
        assert await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p_pending.id) is None

        # 3. Belongs to other buyer -> returns None
        assert await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p_other.id) is None

        # 4. Non-existent purchase_id -> returns None
        assert await get_sender_gift(db, buyer_id=buyer.id, purchase_id=999999) is None


@pytest.mark.asyncio
async def test_has_sender_gifts_lightweight_existence_query(monkeypatch):
    """has_sender_gifts returns boolean indicating if buyer owns any eligible gifts."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        other_user = await _create_test_user(db, telegram_id=1002)
        tariff = await _create_test_tariff(db)

        # Initially no purchases
        assert await has_sender_gifts(db, buyer_id=buyer.id) is False

        # Add an unpaid pending purchase -> still False
        p_pending = GuestPurchase(
            token=TOKEN_1,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PENDING.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
        )
        db.add(p_pending)
        await db.commit()
        assert await has_sender_gifts(db, buyer_id=buyer.id) is False

        # Add a paid gift -> now True
        p_paid = GuestPurchase(
            token=TOKEN_2,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='cabinet',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
        )
        db.add(p_paid)
        await db.commit()
        assert await has_sender_gifts(db, buyer_id=buyer.id) is True

        # other_user has no gifts -> False
        assert await has_sender_gifts(db, buyer_id=other_user.id) is False


@pytest.mark.asyncio
async def test_gift_history_item_immutability_and_artifacts(monkeypatch):
    """GiftHistoryItem is a frozen immutable dataclass that builds canonical claim artifacts."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        p = GuestPurchase(
            token=TOKEN_1,
            buyer_user_id=buyer.id,
            tariff_id=None,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
        )
        db.add(p)
        await db.commit()

        item = await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p.id)
        assert item is not None

        # Frozen instance
        with pytest.raises(FrozenInstanceError):
            item.status = 'delivered'  # type: ignore[misc]

        # Artifacts generation
        artifacts = build_gift_claim_artifacts(
            token=item.token,
            bot_username='test_bot',
            cabinet_url='https://cabinet.example.com',
            share_text='Join us!',
        )
        assert artifacts.public_code == item.public_code
        assert artifacts.bot_claim_url == f'https://t.me/test_bot?start={item.public_code}'
        assert artifacts.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{item.token}'


@pytest.mark.asyncio
async def test_gift_history_service_is_purely_read_only(monkeypatch):
    """History queries must never modify database state or create transactions."""
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _create_test_user(db, telegram_id=1001)
        tariff = await _create_test_tariff(db)
        p = GuestPurchase(
            token=TOKEN_1,
            buyer_user_id=buyer.id,
            tariff_id=tariff.id,
            is_gift=True,
            source='bot',
            status=GuestPurchaseStatus.PAID.value,
            period_days=30,
            amount_kopeks=30000,
            contact_type='telegram',
            contact_value='@buyer',
        )
        db.add(p)
        await db.commit()

        # Run all read queries
        await list_sender_gifts(db, buyer_id=buyer.id)
        await get_sender_gift(db, buyer_id=buyer.id, purchase_id=p.id)
        await has_sender_gifts(db, buyer_id=buyer.id)

        # Confirm no transactions or changes
        tx_count = (await db.execute(select(Transaction))).scalars().all()
        assert len(tx_count) == 0

        # Purchase status remains unchanged
        reloaded = (await db.execute(select(GuestPurchase).where(GuestPurchase.id == p.id))).scalar_one()
        assert reloaded.status == GuestPurchaseStatus.PAID.value
