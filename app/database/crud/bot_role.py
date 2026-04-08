from __future__ import annotations

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BotAdminRole


logger = structlog.get_logger(__name__)


# All valid bot-admin permission sections
BOT_ROLE_SECTIONS = [
    'users',
    'payments',
    'tariffs',
    'subscriptions',
    'promos',
    'broadcasts',
    'servers',
    'support',
    'settings',
    'analytics',
]


class BotRoleCRUD:
    """CRUD operations for bot_admin_roles table."""

    @staticmethod
    async def get_bot_role(db: AsyncSession, user_id: int) -> BotAdminRole | None:
        result = await db.execute(
            select(BotAdminRole).where(BotAdminRole.user_id == user_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def set_bot_role(
        db: AsyncSession,
        user_id: int,
        permissions: list[str],
        created_by: int | None = None,
    ) -> BotAdminRole:
        """Create or update a bot admin role for a user."""
        result = await db.execute(
            select(BotAdminRole).where(BotAdminRole.user_id == user_id)
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            existing.permissions = permissions
            existing.created_by = created_by
            await db.flush()
            await db.refresh(existing)
            logger.info('Updated bot admin role', user_id=user_id, permissions=permissions)
            return existing

        role = BotAdminRole(
            user_id=user_id,
            permissions=permissions,
            created_by=created_by,
        )
        db.add(role)
        await db.flush()
        await db.refresh(role)
        logger.info('Created bot admin role', user_id=user_id, permissions=permissions)
        return role

    @staticmethod
    async def remove_bot_role(db: AsyncSession, user_id: int) -> bool:
        result = await db.execute(
            select(BotAdminRole).where(BotAdminRole.user_id == user_id)
        )
        role = result.scalar_one_or_none()
        if not role:
            return False
        await db.delete(role)
        await db.flush()
        logger.info('Removed bot admin role', user_id=user_id)
        return True

    @staticmethod
    async def list_bot_roles(db: AsyncSession) -> list[BotAdminRole]:
        result = await db.execute(
            select(BotAdminRole).order_by(BotAdminRole.id)
        )
        return list(result.scalars().all())
