"""Shared authenticated gift-claim service for cross-channel activations.

Encapsulates:
- Input parsing and security threshold checks via `app.utils.gift_links`.
- Row-level locking (SELECT ... FOR UPDATE).
- Ownership verification and buyer self-claim rejection.
- First-claimant binding on unowned gifts.
- Status transition (PAID -> PENDING_ACTIVATION -> DELIVERED).
- Idempotent invocation of `activate_purchase`.
- No sensitive token / credential leakage in logs or exceptions.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import GuestPurchase, GuestPurchaseStatus
from app.services import guest_purchase_service
from app.utils.gift_links import InvalidGiftTokenError, parse_gift_claim_input


logger = structlog.get_logger(__name__)


class GiftClaimError(Exception):
    """Base exception for domain-level gift claim errors."""


class GiftClaimNotFoundError(GiftClaimError, ValueError):
    """Raised when a gift cannot be found by token/code, or input is malformed/short."""


class GiftClaimSelfActivationError(GiftClaimError, ValueError):
    """Raised when the buyer attempts to activate their own gift."""


class GiftClaimAlreadyOwnedError(GiftClaimError, ValueError):
    """Raised when a gift is already claimed by or bound to another user."""


class GiftClaimNotActivatableError(GiftClaimError, ValueError):
    """Raised when a gift is in an unclaimable status (e.g. FAILED, PENDING, REFUNDED)."""


async def claim_gift_for_user(
    db: AsyncSession,
    claimant_user_id: int,
    claim_input: str,
    *,
    allow_legacy_short: bool = False,
) -> GuestPurchase:
    """Claim and activate a gift subscription for an authenticated user.

    Args:
        db: AsyncSession database session.
        claimant_user_id: Internal ID of the claiming User (recipient).
        claim_input: Raw code, URL, deep link, or token provided by claimant.
        allow_legacy_short: If True, allows >= 8 char legacy codes (for web cabinet).
            If False (default), enforces strict 48-char security threshold (for Telegram).

    Returns:
        The activated GuestPurchase in DELIVERED status.

    Raises:
        GiftClaimNotFoundError: If input is invalid, malformed, or purchase does not exist.
        GiftClaimSelfActivationError: If the buyer attempts to activate their own gift.
        GiftClaimAlreadyOwnedError: If the gift is already bound to or claimed by someone else.
        GiftClaimNotActivatableError: If the gift is in a non-activatable status.
        GuestPurchaseError: If underlying subscription provisioning fails.
    """
    try:
        token_candidate = parse_gift_claim_input(claim_input, allow_legacy_short=allow_legacy_short)
    except InvalidGiftTokenError as exc:
        raise GiftClaimNotFoundError('Gift not found or invalid claim input') from exc

    if len(token_candidate) >= 64:
        token_filter = GuestPurchase.token == token_candidate
    else:
        token_filter = GuestPurchase.token.startswith(token_candidate)

    result = await db.execute(
        select(GuestPurchase)
        .options(
            selectinload(GuestPurchase.tariff),
            selectinload(GuestPurchase.buyer),
            selectinload(GuestPurchase.user),
        )
        .where(
            token_filter,
            GuestPurchase.is_gift.is_(True),
        )
        .with_for_update()
    )
    purchase = result.scalars().first()

    if purchase is None or not purchase.is_gift:
        raise GiftClaimNotFoundError('Gift not found')

    # Buyer self-activation guard
    if purchase.buyer_user_id is not None and purchase.buyer_user_id == claimant_user_id:
        raise GiftClaimSelfActivationError('Buyer cannot claim their own gift')

    # Ownership guard: already bound/owned by a different user
    if purchase.user_id is not None and purchase.user_id != claimant_user_id:
        raise GiftClaimAlreadyOwnedError('Gift already claimed by another user')

    # Idempotent return if already delivered to the same user
    if purchase.status == GuestPurchaseStatus.DELIVERED.value:
        return purchase

    # Validate activatable status
    activatable_statuses = {
        GuestPurchaseStatus.PENDING_ACTIVATION.value,
        GuestPurchaseStatus.PAID.value,
    }
    if purchase.status not in activatable_statuses:
        raise GiftClaimNotActivatableError('Gift is not in activatable status')

    # Bind unowned gift to claimant
    if purchase.user_id is None:
        purchase.user_id = claimant_user_id

    # Transition PAID -> PENDING_ACTIVATION
    if purchase.status == GuestPurchaseStatus.PAID.value:
        purchase.status = GuestPurchaseStatus.PENDING_ACTIVATION.value

    await db.flush()

    logger.info(
        'Claiming gift for user',
        purchase_id=purchase.id,
        claimant_user_id=claimant_user_id,
    )

    activated_purchase = await guest_purchase_service.activate_purchase(db, purchase.token, skip_notification=True)
    if isinstance(activated_purchase, GuestPurchase):
        return activated_purchase
    return purchase


async def claim_bound_gift_for_user(
    db: AsyncSession,
    claimant_user_id: int,
    purchase_id: int,
) -> GuestPurchase:
    """Activate an already bound gift subscription by purchase ID (directed callback).

    Args:
        db: AsyncSession database session.
        claimant_user_id: Internal ID of the claiming User.
        purchase_id: Primary key of the GuestPurchase row.

    Returns:
        The activated GuestPurchase in DELIVERED status.

    Raises:
        GiftClaimNotFoundError: If purchase does not exist.
        GiftClaimSelfActivationError: If the buyer attempts to activate their own gift.
        GiftClaimAlreadyOwnedError: If the gift is not bound to claimant_user_id.
        GiftClaimNotActivatableError: If the gift is in a non-activatable status.
        GuestPurchaseError: If underlying subscription provisioning fails.
    """
    result = await db.execute(
        select(GuestPurchase)
        .options(
            selectinload(GuestPurchase.tariff),
            selectinload(GuestPurchase.buyer),
            selectinload(GuestPurchase.user),
        )
        .where(
            GuestPurchase.id == purchase_id,
            GuestPurchase.is_gift.is_(True),
        )
        .with_for_update()
    )
    purchase = result.scalars().first()

    if purchase is None or not purchase.is_gift:
        raise GiftClaimNotFoundError('Gift not found')

    # Buyer self-activation guard
    if purchase.buyer_user_id is not None and purchase.buyer_user_id == claimant_user_id:
        raise GiftClaimSelfActivationError('Buyer cannot claim their own gift')

    # Must be bound to claimant
    if purchase.user_id is None or purchase.user_id != claimant_user_id:
        raise GiftClaimAlreadyOwnedError('Gift is not bound to this user')

    # Idempotent return if already delivered to the same user
    if purchase.status == GuestPurchaseStatus.DELIVERED.value:
        return purchase

    # Validate activatable status
    activatable_statuses = {
        GuestPurchaseStatus.PENDING_ACTIVATION.value,
        GuestPurchaseStatus.PAID.value,
    }
    if purchase.status not in activatable_statuses:
        raise GiftClaimNotActivatableError('Gift is not in activatable status')

    # Transition PAID -> PENDING_ACTIVATION if needed
    if purchase.status == GuestPurchaseStatus.PAID.value:
        purchase.status = GuestPurchaseStatus.PENDING_ACTIVATION.value

    await db.flush()

    logger.info(
        'Claiming directed bound gift for user',
        purchase_id=purchase.id,
        claimant_user_id=claimant_user_id,
    )

    activated_purchase = await guest_purchase_service.activate_purchase(db, purchase.token, skip_notification=True)
    if isinstance(activated_purchase, GuestPurchase):
        return activated_purchase
    return purchase
