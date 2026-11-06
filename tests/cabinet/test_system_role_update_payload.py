"""Старый клиент может сохранить permissions, но не менять уровень системной роли."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_roles as m


@pytest.mark.parametrize(
    ('protected', 'allowed'),
    [
        ({}, True),
        ({'level': 50}, True),
        ({'is_active': True}, True),
        ({'level': 50, 'is_active': True}, True),
        ({'level': 51}, False),
        ({'level': 49}, False),
        ({'is_active': False}, False),
        ({'level': None}, False),
        ({'is_active': None}, False),
    ],
)
async def test_system_role_protected_fields_must_not_change(monkeypatch, protected, allowed):
    role = SimpleNamespace(id=9, name='Operator', level=50, is_system=True, is_active=True)
    db, admin = AsyncMock(), SimpleNamespace(id=1)
    update = AsyncMock(return_value=role)
    monkeypatch.setattr(m.AdminRoleCRUD, 'get_by_id', AsyncMock(return_value=role))
    monkeypatch.setattr(m.AdminRoleCRUD, 'update', update)
    monkeypatch.setattr(m, '_get_admin_level', AsyncMock(return_value=1000))
    monkeypatch.setattr(m.UserRoleCRUD, 'get_user_permissions', AsyncMock(return_value=(['users:read'], [], 1000)))
    monkeypatch.setattr(m, '_role_to_response', AsyncMock(return_value={'id': 9}))
    payload = m.RoleUpdateRequest(permissions=['users:read'], **protected)

    if not allowed:
        with pytest.raises(HTTPException) as exc:
            await m.update_role(9, payload, admin, db)
        assert exc.value.status_code == 403
        update.assert_not_awaited()
        db.commit.assert_not_awaited()
        return

    result = await m.update_role(9, payload, admin, db)
    assert result == {'id': 9}
    update.assert_awaited_once_with(db, 9, permissions=['users:read'])
    db.commit.assert_awaited_once()


async def test_non_system_role_can_change_level(monkeypatch):
    role = SimpleNamespace(id=9, level=50, is_system=False)
    update = AsyncMock(return_value=role)
    monkeypatch.setattr(m.AdminRoleCRUD, 'get_by_id', AsyncMock(return_value=role))
    monkeypatch.setattr(m.AdminRoleCRUD, 'update', update)
    monkeypatch.setattr(m, '_get_admin_level', AsyncMock(return_value=1000))
    monkeypatch.setattr(m, '_role_to_response', AsyncMock(return_value={'id': 9}))
    db = AsyncMock()
    await m.update_role(9, m.RoleUpdateRequest(level=51), SimpleNamespace(id=1), db)
    update.assert_awaited_once_with(db, 9, level=51)
