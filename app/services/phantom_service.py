"""Service layer for phantom user claiming and merging.

Phantom users are created during guest landing purchases when Bot.get_chat() fails —
the user record has @username but no telegram_id. When the real user later presses /start,
we match by username and either claim or merge the phantom into their active account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AuditLogCRUD
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import User, UserStatus
from app.services.account_merge_service import execute_merge
from app.services.subscription_service import SubscriptionService
from app.utils.user_utils import generate_unique_referral_code
from app.utils.validators import sanitize_telegram_name


logger = structlog.get_logger(__name__)


async def claim_phantom(
    db: AsyncSession,
    phantom: User,
    *,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    language: str,
    referrer_id: int | None,
) -> tuple[bool, User | None]:
    """Claim a phantom user by backfilling Telegram profile data.

    Commits internally on success; rolls back on IntegrityError.
    Returns (success, user). On IntegrityError falls back to existing user lookup.

    Note: Phantom users created when Bot.get_chat() fails at purchase time are matched
    by username only. Since Telegram usernames are changeable and reassignable, this is
    inherently vulnerable to username change attacks. When Bot.get_chat() succeeds at
    purchase time, telegram_id is stored on the user and the phantom path is not used.
    """
    phantom.telegram_id = telegram_id
    phantom.username = username
    phantom.first_name = sanitize_telegram_name(first_name)
    phantom.last_name = sanitize_telegram_name(last_name)
    phantom.language = language
    phantom.status = UserStatus.ACTIVE.value
    if referrer_id and referrer_id != phantom.id:
        phantom.referred_by_id = referrer_id
    if not phantom.referral_code:
        phantom.referral_code = await generate_unique_referral_code(db, telegram_id)
    phantom.updated_at = datetime.now(UTC)
    phantom.last_activity = datetime.now(UTC)

    # Write audit log in a savepoint — if it fails, the claim mutations are not affected
    try:
        async with db.begin_nested():
            await AuditLogCRUD.create(
                db,
                user_id=phantom.id,
                action='phantom_claimed',
                resource_type='user',
                resource_id=str(phantom.id),
                details={
                    'telegram_id': telegram_id,
                    'username': username,
                },
                status='success',
            )
    except Exception:
        logger.warning('Failed to write phantom claim audit log', phantom_id=phantom.id, exc_info=True)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        logger.warning(
            'IntegrityError claiming phantom user, falling back to existing user lookup',
            phantom_user_id=phantom.id,
            telegram_id=telegram_id,
        )
        existing = await get_user_by_telegram_id(db, telegram_id)
        return False, existing
    await db.refresh(phantom, ['subscriptions'])

    # SECURITY NOTE: Phantom matched by username only (telegram_id was unknown at purchase time).
    # Telegram usernames are changeable/reassignable, so the claimer may not be the intended
    # recipient. This is logged at WARNING for admin audit. A confirmation flow would be needed
    # to fully prevent username spoofing attacks on phantom claims.
    logger.warning(
        'Phantom user claimed by username match (verify intended recipient)',
        phantom_user_id=phantom.id,
        telegram_id=telegram_id,
        username=username,
        has_subscription=bool(phantom.subscriptions),
    )

    # Sync Remnawave panel with updated user data (telegram_id, username, etc.)
    subs = phantom.subscriptions or []
    if subs:
        subscription_service = SubscriptionService()
        for sub in subs:
            try:
                await subscription_service.update_remnawave_user(db, sub)
            except Exception:
                logger.warning(
                    'Failed to update Remnawave panel after phantom claim',
                    phantom_user_id=phantom.id,
                    subscription_id=sub.id,
                    exc_info=True,
                )

    return True, phantom


async def merge_phantom_into_user(
    db: AsyncSession,
    phantom: User,
    active_user: User,
) -> bool:
    """Merge phantom user into active user using the full account merge service.

    Uses execute_merge which handles 30+ tables, subscription transfer, balance,
    unique constraint safety, and soft-deletion. Caller is responsible for commit/rollback.

    Returns True if a subscription was transferred from phantom (caller should sync
    Remnawave panel AFTER commit via ``sync_remnawave_after_phantom_merge``).
    """
    # Determine which subscription to keep: phantom's if active user has none, otherwise active's
    await db.refresh(phantom, ['subscriptions'])
    await db.refresh(active_user, ['subscriptions'])
    phantom_subs = getattr(phantom, 'subscriptions', None) or []
    active_subs = getattr(active_user, 'subscriptions', None) or []
    keep_from: Literal['primary', 'secondary'] = 'secondary' if phantom_subs and not active_subs else 'primary'

    logger.warning(
        'Merging phantom user into active user via execute_merge',
        phantom_id=phantom.id,
        active_user_id=active_user.id,
        keep_subscription_from=keep_from,
        phantom_has_sub=phantom.subscription is not None,
        active_has_sub=active_user.subscription is not None,
    )

    await execute_merge(
        db,
        primary_user_id=active_user.id,
        secondary_user_id=phantom.id,
        keep_subscription_from=keep_from,
        provider='phantom_merge',
    )

    # Durable audit log in a savepoint — if it fails, the merge itself is not affected
    try:
        async with db.begin_nested():
            await AuditLogCRUD.create(
                db,
                user_id=active_user.id,
                action='phantom_merged',
                resource_type='user',
                resource_id=str(phantom.id),
                details={
                    'phantom_id': phantom.id,
                    'active_user_id': active_user.id,
                    'keep_subscription_from': keep_from,
                    'phantom_username': phantom.username,
                },
                status='success',
            )
    except Exception:
        logger.warning(
            'Failed to write phantom merge audit log',
            phantom_id=phantom.id,
            active_user_id=active_user.id,
            exc_info=True,
        )

    return keep_from == 'secondary'


async def sync_remnawave_after_phantom_merge(db: AsyncSession, user: User) -> None:
    """Sync Remnawave panel after a phantom merge that transferred a subscription.

    Must be called AFTER db.commit() to avoid holding FOR UPDATE locks during HTTP calls.
    """
    await db.refresh(user, ['subscriptions'])
    subs = getattr(user, 'subscriptions', None) or []
    if not subs:
        return
    try:
        subscription_service = SubscriptionService()
        for sub in subs:
            await subscription_service.update_remnawave_user(db, sub)
    except Exception:
        logger.warning(
            'Failed to update Remnawave panel after phantom merge',
            user_id=user.id,
            exc_info=True,
        )
