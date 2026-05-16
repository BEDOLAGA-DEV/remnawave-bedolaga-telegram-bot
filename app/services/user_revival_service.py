"""Revival of soft-deleted users back to ACTIVE.

Background
----------
The inactive-user cleanup job (`crud/user.py::get_inactive_users` →
`delete_user`) only flips ``status`` to ``DELETED`` and bumps
``updated_at``. Subscriptions, transactions, referral code, consent
fields — none of that is touched. The row is preserved precisely so a
returning user can be brought back without losing their history.

This module centralises the "flip back to ACTIVE" path used by the
cabinet (Telegram initData login, OAuth provider login, etc.). It is
**intentionally** narrow: no subscription wiping, no referral-code
regeneration, no balance zeroing. Those are appropriate for the bot's
``/start`` flow (``app/handlers/start.py``) because that flow runs a
fresh FSM-driven registration — agreeing to TOS, picking language,
re-asking for referral codes. Cabinet logins do NOT run that FSM, so
applying the wipe would silently drop legitimate state.

Audit
-----
Every revival emits an INFO log with the ``source`` argument so that
admins (and TelegramNotifierProcessor) have a single greppable line
showing where a flip came from. The bot's clean-slate flow is NOT
routed through here — it has its own (more invasive) log line.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, UserStatus


logger = structlog.get_logger(__name__)


class NotDeletedError(RuntimeError):
    """Raised when revive_deleted_user is called on a non-DELETED user.

    A defensive guard — callers should branch on status BEFORE invoking
    revive, never use this as a no-op upgrader.
    """


async def revive_deleted_user(
    db: AsyncSession,
    user: User,
    *,
    source: str,
) -> User:
    """Flip ``user.status`` from DELETED back to ACTIVE.

    **Transaction ownership**: this function NEVER commits. The caller
    is always responsible for committing (or rolling back) the unit of
    work. We mutate fields in-place; the next ``await db.commit()`` on
    the session persists them. This was the architect's call — keeping
    commit control with the caller eliminates the implicit two-mode
    behaviour the earlier ``commit=True/False`` flag invited and makes
    log-vs-state inconsistency impossible (we used to log "revived"
    even when the caller's later commit failed).

    Args:
        db: Active session. NOT used by this function except as part of
            the unused-but-stable signature so callers don't change
            shape if we add IO later. Kept for forward compatibility.
        user: The user row to revive. Must already be loaded; this
            function does NOT re-query.
        source: Short tag describing the call site for audit logging
            (e.g. ``cabinet_dependencies``, ``cabinet_telegram_login``,
            ``oauth_google``). Free-form, but keep it stable so log
            queries stay reliable.

    Returns:
        The same ``user`` instance, with status/last_activity bumped
        in-place. Caller commits.

    Raises:
        NotDeletedError: if the user is not currently DELETED. Callers
            should branch on status FIRST; this guard exists so misuse
            shows up loudly rather than silently no-op'ing.
    """
    if user.status != UserStatus.DELETED.value:
        raise NotDeletedError(
            f'revive_deleted_user called on user {user.id} with status={user.status!r}; '
            'caller must verify status==DELETED before invoking'
        )

    # Touch the session ref so a static analyser doesn't flag `db` as
    # unused — the parameter is intentionally part of the shape (we may
    # add IO here later: an audit-table row, a domain event, etc.).
    _ = db
    now = datetime.now(UTC)
    previous_updated_at = user.updated_at
    user.status = UserStatus.ACTIVE.value
    user.last_activity = now
    user.updated_at = now

    logger.info(
        'User revived from DELETED → ACTIVE',
        user_id=user.id,
        telegram_id=user.telegram_id,
        email=user.email if user.email_verified else None,
        source=source,
        previous_updated_at=previous_updated_at.isoformat() if previous_updated_at else None,
    )
    return user
