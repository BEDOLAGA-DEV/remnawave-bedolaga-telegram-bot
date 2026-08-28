"""Source-neutral sender gift history domain service.

Provides read-only queries for gifts purchased by a buyer across all channels
(Telegram bot, web cabinet, landing pages). Returns immutable GiftHistoryItem
projections to ensure domain isolation beyond database session boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import GuestPurchase, GuestPurchaseStatus
from app.utils.gift_links import build_gift_public_code


if TYPE_CHECKING:
    from app.database.models import User

DEFAULT_HISTORY_LIMIT: int = 10
MIN_HISTORY_LIMIT: int = 1
MAX_HISTORY_LIMIT: int = 500

ELIGIBLE_GIFT_HISTORY_STATUSES: tuple[str, ...] = (
    GuestPurchaseStatus.PAID.value,
    GuestPurchaseStatus.PENDING_ACTIVATION.value,
    GuestPurchaseStatus.DELIVERED.value,
)


def _mask_contact(value: str) -> str:
    """Mask contact value (email or telegram handle) to avoid leaking PII."""
    if not value:
        return ''
    if '@' in value and not value.startswith('@'):
        local, domain = value.rsplit('@', 1)
        return f'{local[:2]}***@{domain}'
    if value.startswith('@'):
        return f'{value[:4]}***' if len(value) > 4 else f'{value[:2]}***'
    return f'{value[:3]}***'


def format_safe_recipient(user: User | None, gift_recipient_value: str | None = None) -> str | None:
    """Format a privacy-safe recipient representation for display.

    Only public @username or masked email are permitted.
    Full names, first/last names, and Telegram IDs are never revealed to the sender.
    If no safe identifier exists, returns None so only status is shown.
    """
    if user is not None:
        if user.username:
            clean_username = user.username.lstrip('@')
            return f'@{clean_username}'
        if user.email:
            return _mask_contact(user.email)
        return None

    if gift_recipient_value:
        if '@' in gift_recipient_value and not gift_recipient_value.startswith('@'):
            return _mask_contact(gift_recipient_value)
        if gift_recipient_value.startswith('@'):
            return gift_recipient_value

    return None


@dataclass(frozen=True, slots=True)
class GiftHistoryItem:
    """Immutable domain projection of a gift purchase for sender history."""

    purchase_id: int
    token: str
    status: str
    tariff_id: int | None
    tariff_name: str | None
    period_days: int
    traffic_limit_gb: int | None
    device_limit: int
    created_at: datetime | None
    paid_at: datetime | None
    delivered_at: datetime | None
    recipient_display: str | None = None
    gift_recipient_type: str | None = None
    gift_recipient_value: str | None = None
    gift_message: str | None = None
    amount_kopeks: int = 0
    currency: str = 'RUB'

    @property
    def public_code(self) -> str:
        """Canonical public gift code (``GIFT_<59_chars>``)."""
        return build_gift_public_code(self.token)

    @property
    def is_claimable(self) -> bool:
        """Whether this gift is in a claimable (unactivated) state."""
        return self.status in (
            GuestPurchaseStatus.PAID.value,
            GuestPurchaseStatus.PENDING_ACTIVATION.value,
        )

    @property
    def is_delivered(self) -> bool:
        """Whether this gift has been claimed and delivered to a recipient."""
        return self.status == GuestPurchaseStatus.DELIVERED.value


def _to_history_item(purchase: GuestPurchase) -> GiftHistoryItem:
    tariff = purchase.tariff
    recipient_display = format_safe_recipient(
        user=purchase.user,
        gift_recipient_value=purchase.gift_recipient_value,
    )
    return GiftHistoryItem(
        purchase_id=purchase.id,
        token=purchase.token,
        status=purchase.status,
        tariff_id=purchase.tariff_id,
        tariff_name=tariff.name if tariff else None,
        period_days=purchase.period_days,
        traffic_limit_gb=tariff.traffic_limit_gb if tariff else None,
        device_limit=tariff.device_limit if tariff else 1,
        created_at=purchase.created_at,
        paid_at=purchase.paid_at,
        delivered_at=purchase.delivered_at,
        recipient_display=recipient_display,
        gift_recipient_type=purchase.gift_recipient_type,
        gift_recipient_value=purchase.gift_recipient_value,
        gift_message=purchase.gift_message,
        amount_kopeks=purchase.amount_kopeks,
        currency=purchase.currency,
    )


async def list_sender_gifts(
    db: AsyncSession,
    buyer_id: int,
    offset: int = 0,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> tuple[list[GiftHistoryItem], int]:
    """Query paginated gift history purchased by the given buyer.

    Source-neutral: includes gifts bought via bot, cabinet, or landing.
    Filters strictly to eligible statuses: PAID, PENDING_ACTIVATION, DELIVERED.
    Orders by created_at DESC, id DESC.

    Args:
        db: Database session.
        buyer_id: User ID of the gift buyer.
        offset: Number of items to skip (non-negative).
        limit: Number of items per page (bounded between MIN_HISTORY_LIMIT and MAX_HISTORY_LIMIT).

    Returns:
        tuple[items, total_count]: List of immutable GiftHistoryItem instances and total count.
    """
    bounded_limit = max(MIN_HISTORY_LIMIT, min(limit, MAX_HISTORY_LIMIT))
    bounded_offset = max(0, offset)

    base_conditions = (
        GuestPurchase.buyer_user_id == buyer_id,
        GuestPurchase.is_gift.is_(True),
        GuestPurchase.status.in_(ELIGIBLE_GIFT_HISTORY_STATUSES),
    )

    # Total count query
    count_query = select(func.count()).select_from(GuestPurchase).where(*base_conditions)
    total_count_result = await db.execute(count_query)
    total_count = total_count_result.scalar_one() or 0

    if total_count == 0 or bounded_offset >= total_count:
        return [], total_count

    # Fetch page items
    items_query = (
        select(GuestPurchase)
        .options(
            selectinload(GuestPurchase.tariff),
            selectinload(GuestPurchase.user),
        )
        .where(*base_conditions)
        .order_by(
            GuestPurchase.created_at.desc(),
            GuestPurchase.id.desc(),
        )
        .offset(bounded_offset)
        .limit(bounded_limit)
    )
    result = await db.execute(items_query)
    purchases = result.scalars().all()

    return [_to_history_item(p) for p in purchases], total_count


async def get_sender_gift(
    db: AsyncSession,
    buyer_id: int,
    purchase_id: int,
) -> GiftHistoryItem | None:
    """Retrieve a single gift purchase owned by the buyer by its ID.

    Args:
        db: Database session.
        buyer_id: User ID of the gift buyer.
        purchase_id: ID of the guest purchase.

    Returns:
        GiftHistoryItem if found and eligible, None otherwise.
    """
    query = (
        select(GuestPurchase)
        .options(
            selectinload(GuestPurchase.tariff),
            selectinload(GuestPurchase.user),
        )
        .where(
            GuestPurchase.id == purchase_id,
            GuestPurchase.buyer_user_id == buyer_id,
            GuestPurchase.is_gift.is_(True),
            GuestPurchase.status.in_(ELIGIBLE_GIFT_HISTORY_STATUSES),
        )
    )
    result = await db.execute(query)
    purchase = result.scalar_one_or_none()
    if purchase is None:
        return None

    return _to_history_item(purchase)


async def has_sender_gifts(
    db: AsyncSession,
    buyer_id: int,
) -> bool:
    """Check whether the buyer has any eligible gift history (lightweight check).

    Used for menu visibility decisions without loading entity relationships.

    Args:
        db: Database session.
        buyer_id: User ID of the gift buyer.

    Returns:
        True if the buyer has at least one eligible gift, False otherwise.
    """
    query = (
        select(GuestPurchase.id)
        .where(
            GuestPurchase.buyer_user_id == buyer_id,
            GuestPurchase.is_gift.is_(True),
            GuestPurchase.status.in_(ELIGIBLE_GIFT_HISTORY_STATUSES),
        )
        .limit(1)
    )
    result = await db.execute(query)
    return result.scalar_one_or_none() is not None
