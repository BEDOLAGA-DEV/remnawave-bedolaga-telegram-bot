"""Security: an admin may only grant permissions they themselves hold.

Privilege-escalation guard. Role create/update/assign previously gated only on
the numeric role LEVEL, so a delegated admin with roles:create / roles:assign
could mint a lower-level role carrying permissions (even *:*) they were never
given and assign it to themselves.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.admin_roles import _ensure_can_grant, _permission_covered
from app.database.crud.rbac import SUPERADMIN_LEVEL


def test_permission_covered_wildcards() -> None:
    assert _permission_covered('roles:create', {'*:*'})
    assert _permission_covered('roles:create', {'roles:*'})
    assert _permission_covered('roles:create', {'roles:create'})
    # Holding a single action does NOT grant the wildcard or sibling actions.
    assert not _permission_covered('roles:delete', {'roles:create'})
    assert not _permission_covered('roles:*', {'roles:create'})
    assert not _permission_covered('*:*', {'roles:*'})


@pytest.mark.asyncio
async def test_cannot_grant_permissions_not_held() -> None:
    held = (['roles:create', 'roles:assign'], [], 100)
    with patch(
        'app.cabinet.routes.admin_roles.UserRoleCRUD.get_user_permissions',
        AsyncMock(return_value=held),
    ):
        for escalation in (['*:*'], ['roles:delete'], ['bulk_actions:execute']):
            with pytest.raises(HTTPException) as exc:
                await _ensure_can_grant(AsyncMock(), MagicMock(id=1), 100, escalation)
            assert exc.value.status_code == status.HTTP_403_FORBIDDEN
        # Granting only held permissions is allowed.
        await _ensure_can_grant(AsyncMock(), MagicMock(id=1), 100, ['roles:create', 'roles:assign'])


@pytest.mark.asyncio
async def test_superadmin_exempt_and_does_not_query() -> None:
    fetch = AsyncMock()
    with patch('app.cabinet.routes.admin_roles.UserRoleCRUD.get_user_permissions', fetch):
        await _ensure_can_grant(AsyncMock(), MagicMock(id=1), SUPERADMIN_LEVEL + 1, ['*:*'])
    fetch.assert_not_awaited()
