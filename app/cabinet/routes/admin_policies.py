"""Admin RBAC access policies management routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AccessPolicyCRUD, AdminRoleCRUD
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/rbac/policies', tags=['Admin RBAC Policies'])


# ============ Schemas ============


class PolicyResponse(BaseModel):
    """Access policy response."""

    id: int
    name: str
    description: str | None = None
    role_id: int | None = None
    role_name: str | None = None
    priority: int
    effect: str
    conditions: dict[str, Any] = Field(default_factory=dict)
    resource: str
    actions: list[str] = Field(default_factory=list)
    is_active: bool
    created_by: int | None = None
    created_at: datetime | None = None


class PolicyCreateRequest(BaseModel):
    """Create a new access policy."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    role_id: int | None = None
    priority: int = Field(default=0, ge=0, le=1000)
    effect: str = Field(pattern=r'^(allow|deny)$')
    conditions: dict[str, Any] = Field(default_factory=dict)
    resource: str = Field(min_length=1, max_length=100)
    actions: list[str] = Field(default_factory=list)


class PolicyUpdateRequest(BaseModel):
    """Update policy fields (all optional)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    role_id: int | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    effect: str | None = Field(default=None, pattern=r'^(allow|deny)$')
    conditions: dict[str, Any] | None = None
    resource: str | None = Field(default=None, min_length=1, max_length=100)
    actions: list[str] | None = None
    is_active: bool | None = None


# ============ Helper Functions ============


async def _policy_to_response(db: AsyncSession, policy) -> PolicyResponse:
    """Convert AccessPolicy model to PolicyResponse with role name."""
    role_name = None
    if policy.role_id is not None:
        role = await AdminRoleCRUD.get_by_id(db, policy.role_id)
        if role:
            role_name = role.name

    return PolicyResponse(
        id=policy.id,
        name=policy.name,
        description=policy.description,
        role_id=policy.role_id,
        role_name=role_name,
        priority=policy.priority,
        effect=policy.effect,
        conditions=policy.conditions or {},
        resource=policy.resource,
        actions=policy.actions or [],
        is_active=policy.is_active,
        created_by=policy.created_by,
        created_at=policy.created_at,
    )


# ============ Routes ============


@router.get('', response_model=list[PolicyResponse])
async def list_policies(
    admin: User = Depends(require_permission('roles:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    role_id: int | None = None,
):
    """List all access policies. Optionally filter by role_id."""
    policies = await AccessPolicyCRUD.get_all(db, role_id=role_id)
    return [await _policy_to_response(db, p) for p in policies]


@router.post('', response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(
    payload: PolicyCreateRequest,
    admin: User = Depends(require_permission('roles:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new access policy (ABAC rule)."""
    # Validate role_id if provided
    if payload.role_id is not None:
        role = await AdminRoleCRUD.get_by_id(db, payload.role_id)
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Referenced role not found',
            )

    policy = await AccessPolicyCRUD.create(
        db,
        name=payload.name,
        description=payload.description,
        role_id=payload.role_id,
        priority=payload.priority,
        effect=payload.effect,
        conditions=payload.conditions,
        resource=payload.resource,
        actions=payload.actions,
        created_by=admin.id,
    )
    await db.commit()

    logger.info(
        'Admin created access policy',
        admin_id=admin.id,
        policy_id=policy.id,
        policy_name=policy.name,
        effect=policy.effect,
    )
    return await _policy_to_response(db, policy)


@router.put('/{policy_id}', response_model=PolicyResponse)
async def update_policy(
    policy_id: int,
    payload: PolicyUpdateRequest,
    admin: User = Depends(require_permission('roles:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update an existing access policy."""
    existing = await AccessPolicyCRUD.get_by_id(db, policy_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Policy not found',
        )

    update_data = payload.model_dump(exclude_unset=True)

    # Validate role_id if changing
    if 'role_id' in update_data and update_data['role_id'] is not None:
        role = await AdminRoleCRUD.get_by_id(db, update_data['role_id'])
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Referenced role not found',
            )

    updated = await AccessPolicyCRUD.update(db, policy_id, **update_data)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Policy not found',
        )

    await db.commit()

    logger.info(
        'Admin updated access policy',
        admin_id=admin.id,
        policy_id=policy_id,
        fields=list(update_data.keys()),
    )
    return await _policy_to_response(db, updated)


@router.delete('/{policy_id}')
async def delete_policy(
    policy_id: int,
    admin: User = Depends(require_permission('roles:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete an access policy."""
    existing = await AccessPolicyCRUD.get_by_id(db, policy_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Policy not found',
        )

    deleted = await AccessPolicyCRUD.delete(db, policy_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to delete policy',
        )

    await db.commit()

    logger.info(
        'Admin deleted access policy',
        admin_id=admin.id,
        policy_id=policy_id,
        policy_name=existing.name,
    )
    return {'message': 'Policy deleted', 'policy_id': policy_id}
