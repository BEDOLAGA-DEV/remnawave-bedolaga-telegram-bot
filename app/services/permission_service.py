"""Permission Engine — RBAC + ABAC evaluation for admin cabinet.

Combines role-based permission checks (fnmatch wildcards) with
attribute-based access policies (time ranges, IP whitelists).
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.rbac import AccessPolicyCRUD, AuditLogCRUD, UserRoleCRUD


if TYPE_CHECKING:
    from app.database.models import AccessPolicy, User


SUPERADMIN_LEVEL = 999


logger = structlog.get_logger(__name__)


def _is_legacy_admin(user: User) -> bool:
    """Check if user is a legacy config-based admin (ADMIN_IDS / ADMIN_EMAILS)."""
    return settings.is_admin(
        telegram_id=user.telegram_id,
        email=user.email if user.email_verified else None,
    )


# ---------------------------------------------------------------------------
# Permission Registry — section -> available actions
# ---------------------------------------------------------------------------

PERMISSION_REGISTRY: dict[str, list[str]] = {
    'users': [
        'read',
        'edit',
        'block',
        'delete',
        'sync',
        'promo_group',
        'balance',
        'subscription',
        'send_offer',
        'referral',
    ],
    'tickets': ['read', 'reply', 'close', 'settings'],
    'stats': ['read', 'export'],
    'broadcasts': ['read', 'create', 'edit', 'delete', 'send'],
    'tariffs': ['read', 'create', 'edit', 'delete'],
    'promocodes': ['read', 'create', 'edit', 'delete', 'stats'],
    'promo_groups': ['read', 'create', 'edit', 'delete'],
    'promo_offers': ['read', 'create', 'edit', 'send'],
    'campaigns': ['read', 'create', 'edit', 'delete', 'stats'],
    'partners': ['read', 'edit', 'approve', 'revoke', 'settings'],
    'withdrawals': ['read', 'approve', 'reject'],
    'payments': ['read', 'edit', 'export'],
    'payment_methods': ['read', 'edit'],
    'servers': ['read', 'edit'],
    'remnawave': ['read', 'sync', 'manage'],
    'traffic': ['read', 'export'],
    'settings': ['read', 'edit'],
    'roles': ['read', 'create', 'edit', 'delete', 'assign'],
    'audit_log': ['read', 'export'],
    'channels': ['read', 'edit'],
    'ban_system': ['read', 'edit', 'ban', 'unban'],
    'wheel': ['read', 'edit'],
    'apps': ['read', 'edit'],
    'email_templates': ['read', 'edit'],
    'pinned_messages': ['read', 'create', 'edit', 'delete'],
    'updates': ['read', 'manage'],
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_all_permissions() -> list[str]:
    """Return flat list of all permissions: ``['users:read', 'users:edit', ...]``."""
    return [f'{section}:{action}' for section, actions in PERMISSION_REGISTRY.items() for action in actions]


def permission_matches(user_perm: str, required_perm: str) -> bool:
    """Check if *user_perm* grants access for *required_perm*.

    Wildcard rules (fnmatch):
    - ``*:*``        matches everything
    - ``users:*``    matches ``users:read``, ``users:edit``, ...
    - ``users:read`` matches only ``users:read``
    """
    return fnmatch(required_perm, user_perm)


# ---------------------------------------------------------------------------
# Internal ABAC helpers
# ---------------------------------------------------------------------------


def _policy_matches_resource(policy: AccessPolicy, required_perm: str) -> bool:
    """Check if an ABAC policy applies to the requested permission.

    ``policy.resource`` is the section pattern (e.g. ``users`` or ``*``).
    ``policy.actions`` is a list of action patterns (e.g. ``['read', '*']``).
    """
    if ':' not in required_perm:
        return False

    section, action = required_perm.split(':', maxsplit=1)

    if not fnmatch(section, policy.resource):
        return False

    policy_actions: list[str] = policy.actions or []
    return any(fnmatch(action, pattern) for pattern in policy_actions)


def _evaluate_conditions(
    conditions: dict | None,
    *,
    ip_address: str | None = None,
) -> bool:
    """Evaluate ABAC conditions dict.  Returns ``True`` when ALL conditions are met.

    Supported keys:
    - ``time_range``: ``{"start": "09:00", "end": "18:00"}`` -- current UTC time
      must fall within the range (inclusive start, exclusive end).
    - ``ip_whitelist``: ``["192.168.1.0/24", "10.0.0.1"]`` -- *ip_address* must
      match at least one entry (CIDR network or exact host).
    - ``max_actions_per_hour``: reserved for future rate-limit logic; always passes.
    """
    if not conditions:
        return True

    # --- time_range ---
    time_range = conditions.get('time_range')
    if time_range is not None:
        now = datetime.now(UTC).time()
        try:
            start = datetime.strptime(time_range['start'], '%H:%M').time()
            end = datetime.strptime(time_range['end'], '%H:%M').time()
        except (KeyError, ValueError) as exc:
            logger.warning('Invalid time_range condition', condition=time_range, error=str(exc))
            return False

        if start <= end:
            # Normal range, e.g. 09:00..18:00
            if not (start <= now < end):
                return False
        # Overnight range, e.g. 22:00..06:00
        elif not (now >= start or now < end):
            return False

    # --- ip_whitelist ---
    ip_whitelist: list[str] | None = conditions.get('ip_whitelist')
    if ip_whitelist is not None:
        if ip_address is None:
            # No IP provided but whitelist required -- deny
            return False

        try:
            client_ip = ipaddress.ip_address(ip_address)
        except ValueError:
            logger.warning('Invalid client IP address', ip_address=ip_address)
            return False

        matched = False
        for entry in ip_whitelist:
            try:
                network = ipaddress.ip_network(entry, strict=False)
                if client_ip in network:
                    matched = True
                    break
            except ValueError:
                logger.warning('Invalid IP whitelist entry', entry=entry)
                continue

        if not matched:
            return False

    # --- max_actions_per_hour (stub) ---
    # Will be implemented with rate-limit counters later.

    return True


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class PermissionService:
    """Stateless permission engine combining RBAC + ABAC evaluation."""

    @staticmethod
    async def check_permission(
        db: AsyncSession,
        user: User,
        required_permission: str,
        *,
        ip_address: str | None = None,
    ) -> tuple[bool, str]:
        """Evaluate whether *user* may perform *required_permission*.

        Returns ``(allowed, reason)`` tuple.

        Algorithm:
        1. Aggregate user permissions via ``UserRoleCRUD.get_user_permissions``.
        2. Check if any RBAC permission matches the required one (fnmatch).
        3. If base RBAC permission is **not** granted -- deny immediately.
        4. Fetch ABAC policies applicable to the user's roles.
        5. Evaluate matching policies in priority order; **deny wins over allow**
           at the same priority level.
        """
        # Step 0 -- legacy config-based admins get full access
        if _is_legacy_admin(user):
            return True, 'Granted by legacy admin config'

        # Step 1 -- aggregate RBAC permissions
        permissions, role_names, max_level = await UserRoleCRUD.get_user_permissions(db, user.id)

        if not permissions:
            logger.debug(
                'Permission denied: no active roles',
                user_id=user.id,
                required=required_permission,
            )
            return False, 'No active roles assigned'

        # Step 2 -- RBAC wildcard matching
        rbac_granted = any(permission_matches(perm, required_permission) for perm in permissions)

        if not rbac_granted:
            logger.debug(
                'Permission denied: RBAC mismatch',
                user_id=user.id,
                required=required_permission,
                permissions=permissions,
            )
            return False, 'Permission not granted by any role'

        # Step 3 -- load ABAC policies for the user's roles
        user_roles = await UserRoleCRUD.get_user_roles(db, user.id)
        role_ids = [ur.role_id for ur in user_roles]
        policies = await AccessPolicyCRUD.get_policies_for_user(db, role_ids)

        if not policies:
            # No ABAC policies -- RBAC alone grants access
            return True, 'Granted by RBAC'

        # Step 4 -- evaluate ABAC policies (highest priority first, already sorted)
        explicit_deny = False
        deny_reason = ''

        for policy in policies:
            if not _policy_matches_resource(policy, required_permission):
                continue

            conditions_met = _evaluate_conditions(
                policy.conditions,
                ip_address=ip_address,
            )
            if not conditions_met:
                # Conditions not satisfied -- this policy does not apply
                continue

            if policy.effect == 'deny':
                explicit_deny = True
                deny_reason = f'Denied by policy: {policy.name}'
                logger.debug(
                    'Permission denied by ABAC policy',
                    user_id=user.id,
                    required=required_permission,
                    policy_id=policy.id,
                    policy_name=policy.name,
                )
                # Deny is final -- stop evaluation
                break

            # effect == 'allow' does not override a prior deny at higher priority,
            # but since policies are sorted desc and deny breaks immediately,
            # reaching here means no deny has fired yet -- just continue.

        if explicit_deny:
            return False, deny_reason

        return True, 'Granted by RBAC + ABAC'

    @staticmethod
    async def get_user_permissions(db: AsyncSession, user_id: int, user: User | None = None) -> dict:
        """Return aggregated permission info for a user.

        Returns::

            {
                'permissions': ['users:read', ...],
                'roles': ['editor', 'moderator'],
                'role_level': 50,
            }
        """
        permissions, role_names, max_level = await UserRoleCRUD.get_user_permissions(db, user_id)

        # Legacy config-based admins get full superadmin permissions
        # Level is SUPERADMIN_LEVEL + 1 so they can manage all roles including level-999
        if user is not None and not permissions and _is_legacy_admin(user):
            permissions = ['*:*']
            role_names = ['superadmin']
            max_level = SUPERADMIN_LEVEL + 1

        return {
            'permissions': permissions,
            'roles': role_names,
            'role_level': max_level,
        }

    @staticmethod
    async def log_action(
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
    ) -> None:
        """Persist an admin audit log entry via ``AuditLogCRUD``."""
        await AuditLogCRUD.create(
            db,
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
