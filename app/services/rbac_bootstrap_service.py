"""
RBAC bootstrap service.

Auto-assigns the Superadmin role to users listed in ADMIN_IDS / ADMIN_EMAILS
config on bot startup. Runs once during the startup sequence.
"""

from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import AdminRole, User, UserRole


logger = structlog.get_logger(__name__)

SUPERADMIN_ROLE_NAME: Final[str] = 'Superadmin'

# Preset roles seeded on first run
_PRESET_ROLES: list[dict] = [
    {
        'name': 'Superadmin',
        'description': 'Full system access',
        'level': 999,
        'permissions': ['*:*'],
        'color': '#EF4444',
        'icon': 'shield',
        'is_system': True,
    },
    {
        'name': 'Admin',
        'description': 'Administrative access',
        'level': 100,
        'permissions': [
            'users:*',
            'tickets:*',
            'stats:*',
            'broadcasts:*',
            'tariffs:*',
            'promocodes:*',
            'promo_groups:*',
            'promo_offers:*',
            'campaigns:*',
            'partners:*',
            'withdrawals:*',
            'payments:*',
            'payment_methods:*',
            'servers:*',
            'remnawave:*',
            'traffic:*',
            'settings:*',
            'roles:read',
            'roles:create',
            'roles:edit',
            'roles:assign',
            'audit_log:*',
            'channels:*',
            'ban_system:*',
            'wheel:*',
            'apps:*',
            'email_templates:*',
            'pinned_messages:*',
            'updates:*',
        ],
        'color': '#F59E0B',
        'icon': 'crown',
        'is_system': True,
    },
    {
        'name': 'Moderator',
        'description': 'User and ticket management',
        'level': 50,
        'permissions': ['users:read', 'users:edit', 'users:block', 'tickets:*', 'ban_system:*'],
        'color': '#3B82F6',
        'icon': 'user-shield',
        'is_system': True,
    },
    {
        'name': 'Marketer',
        'description': 'Marketing tools access',
        'level': 30,
        'permissions': [
            'campaigns:*',
            'broadcasts:*',
            'promocodes:*',
            'promo_offers:*',
            'promo_groups:*',
            'stats:read',
            'pinned_messages:*',
            'wheel:*',
        ],
        'color': '#8B5CF6',
        'icon': 'megaphone',
        'is_system': True,
    },
    {
        'name': 'Support',
        'description': 'Ticket support access',
        'level': 20,
        'permissions': ['tickets:read', 'tickets:reply', 'users:read'],
        'color': '#10B981',
        'icon': 'headset',
        'is_system': True,
    },
]


async def _ensure_preset_roles(db: AsyncSession) -> AdminRole | None:
    """Seed preset roles if they don't exist. Returns the Superadmin role."""
    superadmin_role: AdminRole | None = None

    for preset in _PRESET_ROLES:
        result = await db.execute(select(AdminRole).where(AdminRole.name == preset['name']))
        existing = result.scalar_one_or_none()
        if existing is not None:
            if preset['name'] == SUPERADMIN_ROLE_NAME:
                superadmin_role = existing
            continue

        role = AdminRole(
            name=preset['name'],
            description=preset['description'],
            level=preset['level'],
            permissions=preset['permissions'],
            color=preset['color'],
            icon=preset['icon'],
            is_system=preset['is_system'],
            is_active=True,
        )
        db.add(role)
        await db.flush()
        logger.info('Seeded preset role', role_name=preset['name'], role_id=role.id)

        if preset['name'] == SUPERADMIN_ROLE_NAME:
            superadmin_role = role

    return superadmin_role


async def bootstrap_superadmins(db: AsyncSession) -> None:
    """Ensure every user from ADMIN_IDS / ADMIN_EMAILS has the Superadmin role.

    Also seeds preset roles on first run.
    Idempotent: skips users who already hold an active Superadmin assignment.
    Commits only when at least one change was made.
    """
    try:
        admin_ids = settings.get_admin_ids()
        admin_emails = settings.get_admin_emails()

        # ── 1. Ensure preset roles exist (seeds on first run) ──────────
        superadmin_role = await _ensure_preset_roles(db)

        if superadmin_role is None:
            logger.error('Failed to resolve Superadmin role after seeding')
            return

        if not admin_ids and not admin_emails:
            logger.debug('No admin IDs or emails configured, skipping superadmin assignment')
            await db.commit()
            return

        role_id: int = superadmin_role.id
        assigned_count = 0

        # ── 2. Process admin telegram IDs ──────────────────────────────
        for telegram_id in admin_ids:
            assigned = await _ensure_role_by_telegram_id(db, telegram_id=telegram_id, role_id=role_id)
            if assigned:
                assigned_count += 1

        # ── 3. Process admin emails ────────────────────────────────────
        for email in admin_emails:
            assigned = await _ensure_role_by_email(db, email=email, role_id=role_id)
            if assigned:
                assigned_count += 1

        # ── 4. Commit all changes ──────────────────────────────────────
        await db.commit()

        if assigned_count > 0:
            logger.info(
                'Superadmin bootstrap completed',
                assigned_count=assigned_count,
                role_id=role_id,
            )
        else:
            logger.debug('Superadmin bootstrap: no new assignments needed')

    except Exception:
        await db.rollback()
        logger.exception('Failed to bootstrap superadmins, continuing startup')


async def _ensure_role_by_telegram_id(
    db: AsyncSession,
    *,
    telegram_id: int,
    role_id: int,
) -> bool:
    """Assign Superadmin role to user found by telegram_id. Returns True if assigned."""
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()

    if user is None:
        logger.debug(
            'Admin user not yet registered, skipping',
            telegram_id=telegram_id,
        )
        return False

    return await _assign_if_missing(db, user_id=user.id, role_id=role_id, identifier=str(telegram_id))


async def _ensure_role_by_email(
    db: AsyncSession,
    *,
    email: str,
    role_id: int,
) -> bool:
    """Assign Superadmin role to user found by email (case-insensitive). Returns True if assigned."""
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    user = result.scalar_one_or_none()

    if user is None:
        logger.debug(
            'Admin user (email) not yet registered, skipping',
            email=email,
        )
        return False

    return await _assign_if_missing(db, user_id=user.id, role_id=role_id, identifier=email)


async def _assign_if_missing(
    db: AsyncSession,
    *,
    user_id: int,
    role_id: int,
    identifier: str,
) -> bool:
    """Create or reactivate a UserRole row for this user/role pair.

    Handles the unique constraint on (user_id, role_id) by checking for
    ANY existing assignment (active or inactive) and reactivating if needed.

    Returns True if a new assignment was created or an inactive one was reactivated.
    """
    # Check for ANY existing assignment (active or not) to respect unique constraint
    result = await db.execute(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.is_active:
            logger.debug(
                'User already has Superadmin role',
                user_id=user_id,
                identifier=identifier,
            )
            return False
        # Reactivate previously revoked assignment
        existing.is_active = True
        existing.assigned_at = datetime.now(UTC)
        await db.flush()
        logger.info(
            'Reactivated Superadmin role for user',
            user_id=user_id,
            role_id=role_id,
            identifier=identifier,
            user_role_id=existing.id,
        )
        return True

    user_role = UserRole(
        user_id=user_id,
        role_id=role_id,
        is_active=True,
    )
    db.add(user_role)
    await db.flush()

    logger.info(
        'Assigned Superadmin role to user',
        user_id=user_id,
        role_id=role_id,
        identifier=identifier,
        user_role_id=user_role.id,
    )
    return True
