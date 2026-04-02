import secrets

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import GuestPurchase, GuestPurchaseStatus, LandingPage


logger = structlog.get_logger(__name__)


async def get_landing_by_slug(db: AsyncSession, slug: str) -> LandingPage | None:
    """Get a landing page by its slug."""
    result = await db.execute(select(LandingPage).where(LandingPage.slug == slug))
    return result.scalars().first()


async def get_landing_by_id(db: AsyncSession, landing_id: int) -> LandingPage | None:
    """Get a landing page by its ID."""
    result = await db.execute(select(LandingPage).where(LandingPage.id == landing_id))
    return result.scalars().first()


async def get_active_landing_by_slug(db: AsyncSession, slug: str) -> LandingPage | None:
    """Get an active landing page by its slug."""
    result = await db.execute(
        select(LandingPage).where(
            LandingPage.slug == slug,
            LandingPage.is_active.is_(True),
        )
    )
    return result.scalars().first()


async def get_all_landings(db: AsyncSession) -> list[LandingPage]:
    """Get all landing pages ordered by display_order."""
    result = await db.execute(select(LandingPage).order_by(LandingPage.display_order, LandingPage.id))
    return list(result.scalars().all())


async def create_landing(db: AsyncSession, **kwargs) -> LandingPage:
    """Create a new landing page."""
    landing = LandingPage(**kwargs)
    db.add(landing)
    await db.flush()
    await db.commit()
    await db.refresh(landing)
    logger.info(
        'Created landing page',
        slug=landing.slug,
        landing_id=landing.id,
    )
    return landing


_LANDING_UPDATABLE_FIELDS = frozenset(
    {
        'slug',
        'title',
        'subtitle',
        'is_active',
        'features',
        'footer_text',
        'allowed_tariff_ids',
        'allowed_periods',
        'payment_methods',
        'gift_enabled',
        'custom_css',
        'meta_title',
        'meta_description',
        'display_order',
        'discount_percent',
        'discount_overrides',
        'discount_starts_at',
        'discount_ends_at',
        'discount_badge_text',
        'background_config',
        'analytics_view_enabled',
        'analytics_view_goal',
        'analytics_click_enabled',
        'analytics_click_goal',
    }
)


async def update_landing(db: AsyncSession, landing_id: int, data: dict) -> LandingPage | None:
    """Update a landing page by ID. Returns None if not found."""
    landing = await get_landing_by_id(db, landing_id)
    if landing is None:
        return None

    for key, value in data.items():
        if key in _LANDING_UPDATABLE_FIELDS:
            setattr(landing, key, value)

    await db.commit()
    await db.refresh(landing)
    logger.info(
        'Updated landing page',
        landing_id=landing.id,
        slug=landing.slug,
        updated_fields=list(data.keys()),
    )
    return landing


async def delete_landing(db: AsyncSession, landing_id: int) -> bool:
    """Delete a landing page by ID. Returns True if deleted."""
    landing = await get_landing_by_id(db, landing_id)
    if landing is None:
        return False

    await db.delete(landing)
    await db.commit()
    logger.info(
        'Deleted landing page',
        landing_id=landing_id,
        slug=landing.slug,
    )
    return True


async def update_landing_order(db: AsyncSession, landing_ids: list[int]) -> None:
    """Set display_order for landing pages based on position in list."""
    for order, landing_id in enumerate(landing_ids):
        await db.execute(update(LandingPage).where(LandingPage.id == landing_id).values(display_order=order))
    await db.commit()
    logger.info('Updated landing page order', landing_ids=landing_ids)


def generate_purchase_token() -> str:
    """Generate a cryptographically secure purchase token."""
    return secrets.token_urlsafe(48)


async def create_guest_purchase(db: AsyncSession, *, commit: bool = True, **kwargs) -> GuestPurchase:
    """Create a new guest purchase with an auto-generated token."""
    if 'token' not in kwargs:
        kwargs['token'] = generate_purchase_token()

    purchase = GuestPurchase(**kwargs)
    db.add(purchase)
    await db.flush()
    if commit:
        await db.commit()
    await db.refresh(purchase)
    logger.info(
        'Created guest purchase',
        purchase_id=purchase.id,
        token_prefix=purchase.token[:5],
        status=purchase.status,
        landing_id=purchase.landing_id,
    )
    return purchase


async def get_purchase_by_token(db: AsyncSession, token: str) -> GuestPurchase | None:
    """Get a guest purchase by its token."""
    result = await db.execute(select(GuestPurchase).where(GuestPurchase.token == token))
    return result.scalars().first()


_PURCHASE_UPDATABLE_FIELDS = frozenset(
    {
        'payment_id',
        'paid_at',
        'delivered_at',
        'subscription_url',
        'subscription_crypto_link',
        'user_id',
    }
)


async def update_purchase_status(
    db: AsyncSession,
    token: str,
    status: GuestPurchaseStatus | str,
    *,
    commit: bool = True,
    **extra_fields,
) -> GuestPurchase | None:
    """Update the status of a guest purchase and optional extra fields."""
    purchase = await get_purchase_by_token(db, token)
    if purchase is None:
        return None

    old_status = purchase.status
    purchase.status = status.value if isinstance(status, GuestPurchaseStatus) else status

    for key, value in extra_fields.items():
        if key not in _PURCHASE_UPDATABLE_FIELDS:
            logger.warning('Ignoring disallowed field in purchase update', field=key)
            continue
        setattr(purchase, key, value)

    if commit:
        await db.commit()
        await db.refresh(purchase)
    else:
        await db.flush()

    logger.info(
        'Updated guest purchase status',
        purchase_id=purchase.id,
        token_prefix=token[:5],
        old_status=old_status,
        new_status=purchase.status,
    )
    return purchase


async def get_landing_purchase_stats(db: AsyncSession, landing_id: int) -> dict:
    """Get purchase counts grouped by status for a landing page."""
    result = await db.execute(
        select(
            GuestPurchase.status,
            func.count(GuestPurchase.id),
        )
        .where(GuestPurchase.landing_id == landing_id)
        .group_by(GuestPurchase.status)
    )
    rows = result.all()

    stats = {s.value: 0 for s in GuestPurchaseStatus}
    stats['total'] = 0
    for status_value, count in rows:
        stats[status_value] = count
        stats['total'] += count

    return stats


async def get_all_landing_purchase_stats(db: AsyncSession) -> dict[int, dict]:
    """Get purchase counts grouped by landing_id and status in a single query.

    Returns a dict mapping landing_id -> {status: count, 'total': count}.
    """
    result = await db.execute(
        select(
            GuestPurchase.landing_id,
            GuestPurchase.status,
            func.count(GuestPurchase.id),
        )
        .where(GuestPurchase.landing_id.is_not(None))
        .group_by(GuestPurchase.landing_id, GuestPurchase.status)
    )
    rows = result.all()

    all_stats: dict[int, dict] = {}
    for landing_id, status_value, count in rows:
        if landing_id not in all_stats:
            stats = {s.value: 0 for s in GuestPurchaseStatus}
            stats['total'] = 0
            all_stats[landing_id] = stats
        all_stats[landing_id][status_value] = count
        all_stats[landing_id]['total'] += count

    return all_stats
