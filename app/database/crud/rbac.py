from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import AccessPolicy, AdminAuditLog, AdminRole, User, UserRole


logger = structlog.get_logger(__name__)

# Fields allowed for AdminRole.update()
_ROLE_UPDATABLE_FIELDS = frozenset(
    {
        'name',
        'description',
        'level',
        'permissions',
        'color',
        'icon',
        'is_active',
    }
)

# Fields allowed for AccessPolicy.update()
_POLICY_UPDATABLE_FIELDS = frozenset(
    {
        'name',
        'description',
        'role_id',
        'priority',
        'effect',
        'conditions',
        'resource',
        'actions',
        'is_active',
    }
)

# Superadmin level constant
_SUPERADMIN_LEVEL = 999


class AdminRoleCRUD:
    """CRUD operations for admin_roles table."""

    @staticmethod
    async def get_all(db: AsyncSession, *, include_inactive: bool = False) -> list[AdminRole]:
        """Get all admin roles ordered by level descending."""
        stmt = select(AdminRole).order_by(AdminRole.level.desc())
        if not include_inactive:
            stmt = stmt.where(AdminRole.is_active.is_(True))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(db: AsyncSession, role_id: int) -> AdminRole | None:
        result = await db.execute(select(AdminRole).where(AdminRole.id == role_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_name(db: AsyncSession, name: str) -> AdminRole | None:
        result = await db.execute(select(AdminRole).where(AdminRole.name == name))
        return result.scalar_one_or_none()

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        name: str,
        description: str | None,
        level: int,
        permissions: list[str],
        color: str | None = None,
        icon: str | None = None,
        is_system: bool = False,
        created_by: int | None = None,
    ) -> AdminRole:
        role = AdminRole(
            name=name,
            description=description,
            level=level,
            permissions=permissions,
            color=color,
            icon=icon,
            is_system=is_system,
            created_by=created_by,
        )
        db.add(role)
        await db.flush()
        await db.refresh(role)
        logger.info('Created admin role', role_id=role.id, name=name, level=level)
        return role

    @staticmethod
    async def update(db: AsyncSession, role_id: int, **kwargs: object) -> AdminRole | None:
        """Update only provided fields. Rejects unknown/non-updatable keys."""
        role = await AdminRoleCRUD.get_by_id(db, role_id)
        if not role:
            return None

        for key, value in kwargs.items():
            if key not in _ROLE_UPDATABLE_FIELDS:
                logger.warning('Rejected update of non-updatable AdminRole field', field=key)
                continue
            setattr(role, key, value)

        await db.flush()
        await db.refresh(role)
        logger.info('Updated admin role', role_id=role_id, fields=list(kwargs.keys()))
        return role

    @staticmethod
    async def delete(db: AsyncSession, role_id: int) -> bool:
        """Delete a role. Returns False if the role is a system role or does not exist.

        Cascades are handled by DB-level ON DELETE CASCADE on user_roles and access_policies.
        """
        role = await AdminRoleCRUD.get_by_id(db, role_id)
        if not role:
            return False
        if role.is_system:
            logger.warning('Attempted to delete system role', role_id=role_id, name=role.name)
            return False

        # Explicitly delete dependent user_roles and access_policies in application layer
        # to keep audit trail clear (DB cascade would also work, but explicit is better)
        await db.execute(delete(UserRole).where(UserRole.role_id == role_id))
        await db.execute(delete(AccessPolicy).where(AccessPolicy.role_id == role_id))
        await db.delete(role)
        await db.flush()
        logger.info('Deleted admin role', role_id=role_id, name=role.name)
        return True

    @staticmethod
    async def count_users(db: AsyncSession, role_id: int) -> int:
        """Count active user_roles assigned to this role."""
        result = await db.execute(
            select(func.count(UserRole.id)).where(
                UserRole.role_id == role_id,
                UserRole.is_active.is_(True),
            )
        )
        return result.scalar() or 0


class UserRoleCRUD:
    """CRUD operations for user_roles table + permission aggregation."""

    @staticmethod
    async def get_user_roles(db: AsyncSession, user_id: int) -> list[UserRole]:
        """Get active user roles with eager-loaded AdminRole."""
        result = await db.execute(
            select(UserRole)
            .options(selectinload(UserRole.role))
            .where(
                UserRole.user_id == user_id,
                UserRole.is_active.is_(True),
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_user_permissions(
        db: AsyncSession,
        user_id: int,
    ) -> tuple[list[str], list[str], int]:
        """Aggregate permissions from all active, non-expired roles.

        Returns:
            (sorted_permissions, role_names, max_level)
        """
        now = datetime.now(UTC)
        result = await db.execute(
            select(UserRole)
            .options(selectinload(UserRole.role))
            .where(
                UserRole.user_id == user_id,
                UserRole.is_active.is_(True),
            )
        )
        user_roles = result.scalars().all()

        permissions: set[str] = set()
        role_names: list[str] = []
        max_level: int = 0

        for ur in user_roles:
            # Skip expired assignments
            if ur.expires_at is not None and ur.expires_at <= now:
                continue
            role = ur.role
            if role is None or not role.is_active:
                continue
            permissions.update(role.permissions or [])
            role_names.append(role.name)
            max_level = max(max_level, role.level)

        return sorted(permissions), role_names, max_level

    @staticmethod
    async def assign_role(
        db: AsyncSession,
        *,
        user_id: int,
        role_id: int,
        assigned_by: int | None = None,
        expires_at: datetime | None = None,
    ) -> UserRole:
        """Assign a role to a user. Reactivates existing inactive assignment if present."""
        # Check for existing assignment (active or inactive) due to unique constraint
        result = await db.execute(
            select(UserRole).where(
                UserRole.user_id == user_id,
                UserRole.role_id == role_id,
            )
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            existing.is_active = True
            existing.assigned_by = assigned_by
            existing.assigned_at = datetime.now(UTC)
            existing.expires_at = expires_at
            await db.flush()
            await db.refresh(existing)
            logger.info('Reactivated user role', user_role_id=existing.id, user_id=user_id, role_id=role_id)
            return existing

        user_role = UserRole(
            user_id=user_id,
            role_id=role_id,
            assigned_by=assigned_by,
            expires_at=expires_at,
        )
        db.add(user_role)
        await db.flush()
        await db.refresh(user_role)
        logger.info('Assigned role to user', user_role_id=user_role.id, user_id=user_id, role_id=role_id)
        return user_role

    @staticmethod
    async def revoke_role(db: AsyncSession, user_role_id: int) -> bool:
        """Soft-revoke: set is_active=False. Returns False if not found."""
        result = await db.execute(select(UserRole).where(UserRole.id == user_role_id))
        user_role = result.scalar_one_or_none()
        if not user_role:
            return False

        user_role.is_active = False
        await db.flush()
        logger.info(
            'Revoked user role', user_role_id=user_role_id, user_id=user_role.user_id, role_id=user_role.role_id
        )
        return True

    @staticmethod
    async def get_all_admins(
        db: AsyncSession,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Get users that have at least one active role.

        Returns list of dicts: [{'user': User, 'role_names': [str, ...]}]
        """
        # Subquery: aggregate role names per user
        role_agg = (
            select(
                UserRole.user_id,
                func.array_agg(AdminRole.name).label('role_names'),
            )
            .join(AdminRole, UserRole.role_id == AdminRole.id)
            .where(
                UserRole.is_active.is_(True),
                AdminRole.is_active.is_(True),
            )
            .group_by(UserRole.user_id)
            .subquery()
        )

        stmt = (
            select(User, role_agg.c.role_names)
            .join(role_agg, User.id == role_agg.c.user_id)
            .order_by(User.id)
            .offset(offset)
            .limit(limit)
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [{'user': row[0], 'role_names': list(row[1] or [])} for row in rows]

    @staticmethod
    async def get_superadmin_count(db: AsyncSession) -> int:
        """Count users with an active role at superadmin level (999)."""
        result = await db.execute(
            select(func.count(func.distinct(UserRole.user_id)))
            .join(AdminRole, UserRole.role_id == AdminRole.id)
            .where(
                UserRole.is_active.is_(True),
                AdminRole.is_active.is_(True),
                AdminRole.level == _SUPERADMIN_LEVEL,
            )
        )
        return result.scalar() or 0


class AccessPolicyCRUD:
    """CRUD operations for access_policies table (ABAC)."""

    @staticmethod
    async def get_all(
        db: AsyncSession,
        *,
        role_id: int | None = None,
    ) -> list[AccessPolicy]:
        """Get active policies ordered by priority descending. Optionally filter by role_id."""
        stmt = select(AccessPolicy).where(AccessPolicy.is_active.is_(True)).order_by(AccessPolicy.priority.desc())
        if role_id is not None:
            stmt = stmt.where(AccessPolicy.role_id == role_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_by_id(db: AsyncSession, policy_id: int) -> AccessPolicy | None:
        result = await db.execute(select(AccessPolicy).where(AccessPolicy.id == policy_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def create(db: AsyncSession, **kwargs: object) -> AccessPolicy:
        policy = AccessPolicy(**kwargs)
        db.add(policy)
        await db.flush()
        await db.refresh(policy)
        logger.info('Created access policy', policy_id=policy.id, name=policy.name, effect=policy.effect)
        return policy

    @staticmethod
    async def update(db: AsyncSession, policy_id: int, **kwargs: object) -> AccessPolicy | None:
        """Update only provided fields. Rejects unknown/non-updatable keys."""
        policy = await AccessPolicyCRUD.get_by_id(db, policy_id)
        if not policy:
            return None

        for key, value in kwargs.items():
            if key not in _POLICY_UPDATABLE_FIELDS:
                logger.warning('Rejected update of non-updatable AccessPolicy field', field=key)
                continue
            setattr(policy, key, value)

        await db.flush()
        await db.refresh(policy)
        logger.info('Updated access policy', policy_id=policy_id, fields=list(kwargs.keys()))
        return policy

    @staticmethod
    async def delete(db: AsyncSession, policy_id: int) -> bool:
        policy = await AccessPolicyCRUD.get_by_id(db, policy_id)
        if not policy:
            return False
        await db.delete(policy)
        await db.flush()
        logger.info('Deleted access policy', policy_id=policy_id, name=policy.name)
        return True

    @staticmethod
    async def get_policies_for_user(
        db: AsyncSession,
        role_ids: list[int],
    ) -> list[AccessPolicy]:
        """Get active policies matching any of the given role_ids OR global (role_id IS NULL).

        Ordered by priority descending for correct evaluation order.
        """
        if not role_ids:
            # Only global policies
            stmt = (
                select(AccessPolicy)
                .where(
                    AccessPolicy.is_active.is_(True),
                    AccessPolicy.role_id.is_(None),
                )
                .order_by(AccessPolicy.priority.desc())
            )
        else:
            stmt = (
                select(AccessPolicy)
                .where(
                    AccessPolicy.is_active.is_(True),
                    or_(
                        AccessPolicy.role_id.in_(role_ids),
                        AccessPolicy.role_id.is_(None),
                    ),
                )
                .order_by(AccessPolicy.priority.desc())
            )
        result = await db.execute(stmt)
        return list(result.scalars().all())


class AuditLogCRUD:
    """Create + filtered query for admin_audit_log table."""

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        user_id: int,
        action: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        status: str = 'success',
        request_method: str | None = None,
        request_path: str | None = None,
    ) -> AdminAuditLog:
        entry = AdminAuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
            status=status,
            request_method=request_method,
            request_path=request_path,
        )
        db.add(entry)
        await db.flush()
        await db.refresh(entry)
        logger.debug(
            'Audit log created',
            audit_id=entry.id,
            user_id=user_id,
            action=action,
            status=status,
        )
        return entry

    @staticmethod
    async def get_logs(
        db: AsyncSession,
        *,
        user_id: int | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        load_user: bool = False,
    ) -> tuple[list[AdminAuditLog], int]:
        """Get filtered audit logs with total count.

        Returns:
            (logs, total_count)
        """
        filters = []
        if user_id is not None:
            filters.append(AdminAuditLog.user_id == user_id)
        if action is not None:
            filters.append(AdminAuditLog.action.ilike(f'%{action}%'))
        if resource_type is not None:
            filters.append(AdminAuditLog.resource_type == resource_type)
        if status is not None:
            filters.append(AdminAuditLog.status == status)
        if date_from is not None:
            filters.append(AdminAuditLog.created_at >= date_from)
        if date_to is not None:
            filters.append(AdminAuditLog.created_at <= date_to)

        where_clause = and_(*filters) if filters else True

        # Total count
        count_result = await db.execute(select(func.count(AdminAuditLog.id)).where(where_clause))
        total_count = count_result.scalar() or 0

        # Paginated results
        stmt = (
            select(AdminAuditLog)
            .where(where_clause)
            .order_by(AdminAuditLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if load_user:
            from sqlalchemy.orm import selectinload

            stmt = stmt.options(selectinload(AdminAuditLog.user))
        result = await db.execute(stmt)
        logs = list(result.scalars().all())

        return logs, total_count
