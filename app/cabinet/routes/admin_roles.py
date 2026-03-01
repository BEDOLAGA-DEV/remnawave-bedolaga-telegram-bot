"""Admin RBAC roles management routes."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AdminRoleCRUD, UserRoleCRUD
from app.database.models import User
from app.services.permission_service import PERMISSION_REGISTRY, get_all_permissions

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/rbac', tags=['Admin RBAC'])


# ============ Schemas ============


class RoleResponse(BaseModel):
    """Admin role with user count."""

    id: int
    name: str
    description: str | None = None
    level: int
    permissions: list[str] = Field(default_factory=list)
    color: str | None = None
    icon: str | None = None
    is_system: bool
    is_active: bool
    user_count: int = 0
    created_at: datetime | None = None


class RoleCreateRequest(BaseModel):
    """Create a new custom role."""

    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    level: int = Field(ge=0, le=998)
    permissions: list[str] = Field(default_factory=list)
    color: str | None = Field(default=None, max_length=7)
    icon: str | None = Field(default=None, max_length=50)


class RoleUpdateRequest(BaseModel):
    """Update role fields (all optional)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    level: int | None = Field(default=None, ge=0, le=998)
    permissions: list[str] | None = None
    color: str | None = Field(default=None, max_length=7)
    icon: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None


class RoleAssignRequest(BaseModel):
    """Assign a role to a user."""

    user_id: int
    role_id: int
    expires_at: datetime | None = None


class PermissionSection(BaseModel):
    """Permission section with available actions."""

    section: str
    actions: list[str]


class UserRoleResponse(BaseModel):
    """User-role assignment details."""

    id: int
    user_id: int
    role_id: int
    role_name: str | None = None
    user_telegram_id: int | None = None
    user_username: str | None = None
    user_first_name: str | None = None
    user_email: str | None = None
    assigned_by: int | None = None
    assigned_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool


class AdminWithRolesResponse(BaseModel):
    """User that has at least one admin role."""

    user_id: int
    telegram_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    role_names: list[str] = Field(default_factory=list)


# ============ Helper Functions ============


async def _role_to_response(db: AsyncSession, role) -> RoleResponse:
    """Convert AdminRole model to RoleResponse with user count."""
    user_count = await AdminRoleCRUD.count_users(db, role.id)
    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        level=role.level,
        permissions=role.permissions or [],
        color=role.color,
        icon=role.icon,
        is_system=role.is_system,
        is_active=role.is_active,
        user_count=user_count,
        created_at=role.created_at,
    )


async def _get_admin_level(db: AsyncSession, admin: User) -> int:
    """Get the maximum role level of the current admin.

    Legacy config-based admins (ADMIN_IDS) get superadmin level (999+1=1000)
    so they can manage all roles including level 999.
    """
    from app.config import settings

    _perms, _names, max_level = await UserRoleCRUD.get_user_permissions(db, admin.id)

    # Legacy config-based admins always get the highest level
    if settings.is_admin(
        telegram_id=admin.telegram_id,
        email=admin.email if admin.email_verified else None,
    ):
        max_level = max(max_level, 1000)

    return max_level


def _validate_permissions(permissions: list[str]) -> None:
    """Validate that all provided permissions exist in the registry."""
    all_valid = set(get_all_permissions())
    # Also allow wildcard patterns
    all_valid.add('*:*')
    for section in PERMISSION_REGISTRY:
        all_valid.add(f'{section}:*')

    invalid = [p for p in permissions if p not in all_valid]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid permissions: {", ".join(invalid)}',
        )


# ============ Routes ============


@router.get('/permissions', response_model=list[PermissionSection])
async def get_permission_registry(
    admin: User = Depends(require_permission('roles:read')),
):
    """Get all available permissions grouped by section."""
    return [
        PermissionSection(section=section, actions=list(actions)) for section, actions in PERMISSION_REGISTRY.items()
    ]


@router.get('/roles/{role_id}/users', response_model=list[UserRoleResponse])
async def list_role_users(
    role_id: int,
    admin: User = Depends(require_permission('roles:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List user-role assignments for a specific role."""
    from sqlalchemy.orm import selectinload as _sel

    role = await AdminRoleCRUD.get_by_id(db, role_id)
    if not role:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Role not found')

    from sqlalchemy import select as _sa_select

    from app.database.models import UserRole as _UserRole

    result = await db.execute(
        _sa_select(_UserRole)
        .options(_sel(_UserRole.user), _sel(_UserRole.role))
        .where(_UserRole.role_id == role_id, _UserRole.is_active.is_(True))
        .order_by(_UserRole.assigned_at.desc())
    )
    assignments = result.scalars().all()

    return [
        UserRoleResponse(
            id=a.id,
            user_id=a.user_id,
            role_id=a.role_id,
            role_name=a.role.name if a.role else None,
            user_telegram_id=a.user.telegram_id if a.user else None,
            user_username=a.user.username if a.user else None,
            user_first_name=a.user.first_name if a.user else None,
            user_email=a.user.email if a.user else None,
            assigned_by=a.assigned_by,
            assigned_at=a.assigned_at,
            expires_at=a.expires_at,
            is_active=a.is_active,
        )
        for a in assignments
    ]


@router.get('/roles', response_model=list[RoleResponse])
async def list_roles(
    admin: User = Depends(require_permission('roles:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    include_inactive: bool = False,
):
    """List all admin roles with user counts."""
    roles = await AdminRoleCRUD.get_all(db, include_inactive=include_inactive)
    return [await _role_to_response(db, role) for role in roles]


@router.post('/roles', response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreateRequest,
    admin: User = Depends(require_permission('roles:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new custom admin role."""
    # Validate permissions list
    _validate_permissions(payload.permissions)

    # Hierarchy enforcement: cannot create role with level >= own level
    admin_level = await _get_admin_level(db, admin)
    if payload.level >= admin_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot create a role with level >= your own role level',
        )

    # Check name uniqueness
    existing = await AdminRoleCRUD.get_by_name(db, payload.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Role with this name already exists',
        )

    role = await AdminRoleCRUD.create(
        db,
        name=payload.name,
        description=payload.description,
        level=payload.level,
        permissions=payload.permissions,
        color=payload.color,
        icon=payload.icon,
        created_by=admin.id,
    )
    await db.commit()

    logger.info('Admin created role', admin_id=admin.id, role_id=role.id, role_name=role.name)
    return await _role_to_response(db, role)


@router.put('/roles/{role_id}', response_model=RoleResponse)
async def update_role(
    role_id: int,
    payload: RoleUpdateRequest,
    admin: User = Depends(require_permission('roles:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update an existing admin role."""
    role = await AdminRoleCRUD.get_by_id(db, role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Role not found',
        )

    admin_level = await _get_admin_level(db, admin)

    # Cannot edit a role at or above own level
    if role.level >= admin_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot edit a role at or above your own level',
        )

    update_data = payload.model_dump(exclude_unset=True)

    # Validate level change
    if 'level' in update_data and update_data['level'] >= admin_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot set role level >= your own role level',
        )

    # Validate permissions
    if 'permissions' in update_data and update_data['permissions'] is not None:
        _validate_permissions(update_data['permissions'])

    # Check name uniqueness if name is changing
    if 'name' in update_data and update_data['name'] != role.name:
        existing = await AdminRoleCRUD.get_by_name(db, update_data['name'])
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Role with this name already exists',
            )

    updated = await AdminRoleCRUD.update(db, role_id, **update_data)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Role not found',
        )

    await db.commit()

    logger.info('Admin updated role', admin_id=admin.id, role_id=role_id, fields=list(update_data.keys()))
    return await _role_to_response(db, updated)


@router.delete('/roles/{role_id}')
async def delete_role(
    role_id: int,
    admin: User = Depends(require_permission('roles:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a custom admin role. System roles cannot be deleted."""
    role = await AdminRoleCRUD.get_by_id(db, role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Role not found',
        )

    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot delete a system role',
        )

    admin_level = await _get_admin_level(db, admin)
    if role.level >= admin_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot delete a role at or above your own level',
        )

    deleted = await AdminRoleCRUD.delete(db, role_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to delete role',
        )

    await db.commit()

    logger.info('Admin deleted role', admin_id=admin.id, role_id=role_id, role_name=role.name)
    return {'message': 'Role deleted', 'role_id': role_id}


@router.post('/assignments', response_model=UserRoleResponse, status_code=status.HTTP_201_CREATED)
async def assign_role(
    payload: RoleAssignRequest,
    admin: User = Depends(require_permission('roles:assign')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Assign a role to a user. Hierarchy enforcement applies."""
    role = await AdminRoleCRUD.get_by_id(db, payload.role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Role not found',
        )

    admin_level = await _get_admin_level(db, admin)

    # Cannot assign a role with level >= own level
    if role.level >= admin_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot assign a role with level >= your own role level',
        )

    # Verify target user exists
    from app.database.crud.user import get_user_by_id

    target_user = await get_user_by_id(db, payload.user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Target user not found',
        )

    user_role = await UserRoleCRUD.assign_role(
        db,
        user_id=payload.user_id,
        role_id=payload.role_id,
        assigned_by=admin.id,
        expires_at=payload.expires_at,
    )
    await db.commit()

    logger.info(
        'Admin assigned role',
        admin_id=admin.id,
        target_user_id=payload.user_id,
        role_id=payload.role_id,
        role_name=role.name,
    )
    return UserRoleResponse(
        id=user_role.id,
        user_id=user_role.user_id,
        role_id=user_role.role_id,
        role_name=role.name,
        user_telegram_id=target_user.telegram_id,
        user_username=target_user.username,
        user_first_name=target_user.first_name,
        user_email=target_user.email,
        assigned_by=user_role.assigned_by,
        assigned_at=user_role.assigned_at,
        expires_at=user_role.expires_at,
        is_active=user_role.is_active,
    )


@router.delete('/assignments/{assignment_id}')
async def revoke_role(
    assignment_id: int,
    admin: User = Depends(require_permission('roles:assign')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Revoke a role assignment. Cannot remove the last superadmin."""
    from sqlalchemy import select as sa_select

    from app.database.models import UserRole

    # Load the assignment to check hierarchy
    result = await db.execute(sa_select(UserRole).where(UserRole.id == assignment_id))
    user_role = result.scalar_one_or_none()
    if not user_role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Role assignment not found',
        )

    role = await AdminRoleCRUD.get_by_id(db, user_role.role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Associated role not found',
        )

    admin_level = await _get_admin_level(db, admin)

    # Cannot revoke a role at or above own level
    if role.level >= admin_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot revoke a role at or above your own level',
        )

    # Protect last superadmin (level 999)
    superadmin_level = 999
    if role.level == superadmin_level:
        superadmin_count = await UserRoleCRUD.get_superadmin_count(db)
        if superadmin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='Cannot remove the last superadmin',
            )

    revoked = await UserRoleCRUD.revoke_role(db, assignment_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to revoke role',
        )

    await db.commit()

    logger.info(
        'Admin revoked role assignment',
        admin_id=admin.id,
        assignment_id=assignment_id,
        target_user_id=user_role.user_id,
        role_name=role.name,
    )
    return {'message': 'Role revoked', 'assignment_id': assignment_id}
