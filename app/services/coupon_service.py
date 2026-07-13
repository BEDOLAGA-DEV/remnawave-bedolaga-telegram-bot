"""Wholesale coupons: batch-generated one-time links that grant subscription days.

The admin creates a batch of coupons for a tariff+period and hands the deep
links (``https://t.me/<bot>?start=coupon_<token>``) to a partner. Redeeming a
link grants the batch tariff for N days, or extends an existing subscription
by N days — mirroring gift activation semantics
(:func:`app.services.guest_purchase_service.activate_purchase`).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.coupon import get_coupon_by_token
from app.database.crud.subscription import (
    create_paid_subscription,
    extend_subscription,
    get_subscription_by_user_id,
    replace_subscription,
)
from app.database.models import Coupon, CouponStatus, Subscription, Tariff, User, _aware
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

COUPON_DEEP_LINK_PREFIX = 'coupon_'

# 32 hex chars = 128 bits: unguessable, and the full deep-link payload
# (`coupon_` + token = 39 chars) fits Telegram's 64-char start param without
# truncation, so lookups are always exact-match (no prefix matching at all).
_COUPON_TOKEN_RE = re.compile(r'[0-9a-f]{32}')


def is_coupon_token(value: str) -> bool:
    """True if ``value`` has the exact coupon-token format (no DB hit)."""
    return bool(_COUPON_TOKEN_RE.fullmatch(value))


def build_coupon_deeplink(bot_username: str, token: str) -> str:
    """The single source of truth for the coupon activation deep link.

    Callers guard the empty-``bot_username`` case themselves (the username is
    not known until the bot has synced its identity).
    """
    return f'https://t.me/{bot_username}?start={COUPON_DEEP_LINK_PREFIX}{token}'


class CouponRedemptionError(Exception):
    """Coupon cannot be redeemed; ``code`` selects the user-facing message.

    Codes: ``invalid`` (unknown / revoked / redeemed by someone else —
    deliberately uniform so the response is not an oracle), ``expired``,
    ``already_redeemed_by_you``, ``internal``.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class CouponRedemptionResult:
    tariff_name: str
    period_days: int
    renewed: bool  # True — продлили действующую подписку, False — выдали новую/заменили истёкшую
    end_date: datetime | None
    traffic_limit_gb: int | None
    device_limit: int | None


def _check_redeemable(coupon: Coupon, user: User) -> None:
    """Raise CouponRedemptionError unless the coupon can be redeemed by ``user``.

    Pure checks only — safe to run both before and after the row lock is taken.
    """
    if coupon.status == CouponStatus.REDEEMED.value:
        if coupon.redeemed_by == user.id:
            raise CouponRedemptionError('already_redeemed_by_you')
        raise CouponRedemptionError('invalid')
    if coupon.status != CouponStatus.ACTIVE.value:
        # REVOKED (or any future state): same uniform answer as "not found"
        raise CouponRedemptionError('invalid')

    batch = coupon.batch
    if batch is None:
        raise CouponRedemptionError('invalid')
    if batch.is_expired:
        raise CouponRedemptionError('expired')


async def redeem_coupon(db: AsyncSession, token: str, user: User) -> CouponRedemptionResult:
    """Atomically redeem a one-time coupon for ``user``.

    All validation runs on an unlocked read, so a rejected link never holds a
    row lock for the rest of the caller's handler. The claim itself re-reads
    the row with SELECT ... FOR UPDATE and re-checks it, and the status flip
    to REDEEMED is set BEFORE the Remnawave sync — which commits the session
    internally — so claim and grant land in one transaction and a coupon can
    never pay out twice.
    """
    normalized = (token or '').strip().lower()
    if not is_coupon_token(normalized):
        raise CouponRedemptionError('invalid')

    coupon = await get_coupon_by_token(db, normalized)
    if coupon is None:
        raise CouponRedemptionError('invalid')

    _check_redeemable(coupon, user)

    tariff = coupon.batch.tariff
    if tariff is None:
        logger.error('Купон ссылается на отсутствующий тариф', coupon_id=coupon.id, batch_id=coupon.batch_id)
        raise CouponRedemptionError('internal')

    tariff_name = tariff.name
    period_days = coupon.batch.period_days
    batch_id = coupon.batch_id
    coupon_id = coupon.id

    # Claim under row lock: reload the row FOR UPDATE and re-check — a
    # concurrent redemption or batch revoke may have won between the two reads.
    await db.refresh(coupon, with_for_update=True)
    _check_redeemable(coupon, user)

    try:
        subscription, renewed = await _grant_subscription_days(db, user, tariff, period_days)
        end_date = subscription.end_date
        traffic_limit_gb = subscription.traffic_limit_gb
        device_limit = subscription.device_limit

        coupon.status = CouponStatus.REDEEMED.value
        coupon.redeemed_by = user.id
        coupon.redeemed_at = datetime.now(UTC)

        # create_remnawave_user() commits the session internally on success
        # (persisting claim+grant atomically) and swallows panel/API errors,
        # returning None WITHOUT committing — treat that as a failure so the
        # rollback below keeps the coupon ACTIVE and the link retryable.
        remnawave_user = await SubscriptionService().create_remnawave_user(db, subscription)
        if remnawave_user is None:
            raise RuntimeError('Remnawave sync failed')
        # Explicit final commit (belt-and-suspenders): guarantees claim+grant
        # persist even if create_remnawave_user's internal-commit behaviour ever
        # changes. Do not remove without re-auditing that contract.
        await db.commit()
    except Exception:
        logger.exception('Не удалось погасить купон', coupon_id=coupon_id, user_id=user.id)
        await db.rollback()
        raise CouponRedemptionError('internal') from None

    logger.info(
        'Купон погашен',
        coupon_id=coupon_id,
        batch_id=batch_id,
        user_id=user.id,
        days=period_days,
    )
    return CouponRedemptionResult(
        tariff_name=tariff_name,
        period_days=period_days,
        renewed=renewed,
        end_date=end_date,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
    )


async def _grant_subscription_days(
    db: AsyncSession, user: User, tariff: Tariff, period_days: int
) -> tuple[Subscription, bool]:
    """Create/extend/replace a subscription for ``period_days`` of ``tariff``.

    Returns ``(subscription, renewed)``: ``renewed`` is True when an active
    subscription was extended, False when a new one was created or an expired
    one replaced. Mirrors the gift-activation branches in ``activate_purchase``.
    All CRUD calls use ``commit=False`` — the caller owns the transaction.
    """
    squads = list(tariff.allowed_squads or [])
    if not squads:
        from app.database.crud.server_squad import get_all_server_squads

        # Explicit high limit: the default (50) silently truncates deployments
        # with more available squads.
        all_servers, _ = await get_all_server_squads(db, available_only=True, limit=10_000)
        squads = [s.squad_uuid for s in all_servers if s.squad_uuid]

    if settings.is_multi_tariff_enabled():
        from app.database.crud.subscription import get_subscription_by_user_and_tariff

        existing = await get_subscription_by_user_and_tariff(db, user.id, tariff.id)
    else:
        existing = await get_subscription_by_user_id(db, user.id)

    has_time = existing is not None and existing.end_date is not None and _aware(existing.end_date) > datetime.now(UTC)

    if existing is not None and has_time:
        # In multi-tariff mode the subscription already belongs to this tariff,
        # so passing tariff_id is a no-op; in single mode it switches the
        # subscription to the batch tariff (same as gift activation).
        subscription = await extend_subscription(
            db,
            existing,
            period_days,
            tariff_id=tariff.id,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=tariff.device_limit,
            connected_squads=squads,
            commit=False,
        )
        return subscription, True

    if existing is not None:
        # Expired subscription — replace with fresh dates
        subscription = await replace_subscription(
            db,
            existing,
            duration_days=period_days,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=tariff.device_limit,
            connected_squads=squads,
            is_trial=False,
            update_server_counters=True,
            commit=False,
        )
        subscription.tariff_id = tariff.id
        return subscription, False

    subscription = await create_paid_subscription(
        db=db,
        user_id=user.id,
        duration_days=period_days,
        traffic_limit_gb=tariff.traffic_limit_gb,
        device_limit=tariff.device_limit,
        connected_squads=squads,
        tariff_id=tariff.id,
        update_server_counters=True,
        commit=False,
    )
    return subscription, False
