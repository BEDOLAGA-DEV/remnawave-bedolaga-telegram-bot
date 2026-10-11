"""Shared post-deposit pipeline for every successful balance credit."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User


logger = structlog.get_logger(__name__)


async def after_successful_deposit(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    *,
    bot: Any | None = None,
    was_first_topup: bool,
    apply_bonuses: bool = True,
    notify_email: bool = True,
) -> None:
    """Run referral + first-topup flag + cart/auto-extend after money is committed.

    Never raises: the credit itself already succeeded; callers must not retry it
    because of a side-effect failure.

    Transaction contract:
    - This helper does **not** call ``db.commit()`` or ``db.rollback()``.
    - It may mutate ``user.has_made_first_topup`` in memory (and refresh ``user``).
    - ``process_referral_topup`` may still commit on its own (existing behaviour).
    - The caller must ``await db.commit()`` after this returns if they need the
      first-topup flag (and any other in-session mutations) persisted, especially
      when invoking from an already-open transaction.
    """
    if not apply_bonuses or amount_kopeks <= 0:
        return

    try:
        await _apply_referral_and_first_topup(
            db, user, amount_kopeks, was_first_topup=was_first_topup, bot=bot
        )
    except Exception as error:
        logger.error(
            'Post-deposit referral failed',
            user_id=getattr(user, 'id', None),
            amount_kopeks=amount_kopeks,
            error=error,
            exc_info=True,
        )

    try:
        from app.services.payment.common import send_cart_notification_after_topup

        await send_cart_notification_after_topup(
            user, amount_kopeks, db, bot, notify_email=notify_email
        )
    except Exception as error:
        logger.error(
            'Post-deposit autopay/cart failed',
            user_id=getattr(user, 'id', None),
            amount_kopeks=amount_kopeks,
            error=error,
            exc_info=True,
        )


async def _apply_referral_and_first_topup(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    *,
    was_first_topup: bool,
    bot: Any | None,
) -> None:
    try:
        from app.services.referral_service import process_referral_topup

        await process_referral_topup(db, user.id, amount_kopeks, bot)
    except Exception as error:
        logger.error(
            'process_referral_topup failed',
            user_id=user.id,
            amount_kopeks=amount_kopeks,
            error=error,
            exc_info=True,
        )

    await db.refresh(user)
    # Referrals: flag is set inside process_referral_topup when deferred bonus pays out.
    # Non-referrals still need the flag for first-topup stats/thresholds.
    # Caller owns commit — see after_successful_deposit docstring.
    if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
        user.has_made_first_topup = True
