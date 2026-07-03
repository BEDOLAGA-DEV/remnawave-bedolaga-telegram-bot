from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status

from app.cabinet import dependencies as cabinet_dependencies
from app.cabinet.routes import admin_mobile
from app.cabinet.schemas.admin_mobile import CONTRACT_VERSION
from app.cabinet.schemas.auth import AuthResponse, TokenResponse, UserResponse
from app.services.permission_service import permission_matches


class _User:
    id = 42
    telegram_id = 4242
    email = 'admin@example.test'
    email_verified = True


class _Db:
    def __init__(self) -> None:
        self.commit = AsyncMock()


async def _set_current_roles(monkeypatch: pytest.MonkeyPatch, roles: list[str], permissions: list[str]) -> None:
    async def fake_get_user_permissions(_db, user_id: int):
        assert user_id == _User.id
        return permissions, roles, 100 if roles else 0

    async def fake_check_permission(_db, _user, required_permission: str, *, ip_address=None):
        if any(permission_matches(current_permission, required_permission) for current_permission in permissions):
            return True, 'Granted by RBAC'
        return False, 'Permission not granted by any role'

    async def fake_log_action(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        'app.database.crud.rbac.UserRoleCRUD.get_user_permissions',
        fake_get_user_permissions,
    )
    monkeypatch.setattr('app.services.permission_service.PermissionService.check_permission', fake_check_permission)
    monkeypatch.setattr('app.services.permission_service.PermissionService.log_action', fake_log_action)


@pytest.mark.asyncio
@pytest.mark.parametrize('role_name', ['Superadmin', 'Admin', 'Moderator'])
async def test_mobile_admin_allowed_roles_with_required_permission_succeed(
    monkeypatch: pytest.MonkeyPatch,
    role_name: str,
) -> None:
    await _set_current_roles(monkeypatch, [role_name], ['users:*'])

    permissions, roles, _level = await cabinet_dependencies.ensure_mobile_admin_access(_Db(), _User(), 'users:read')

    assert permissions == ['users:*']
    assert roles == [role_name]


@pytest.mark.asyncio
@pytest.mark.parametrize('role_name', ['Support', 'Marketer'])
async def test_mobile_admin_disallowed_roles_are_rejected_even_with_matching_permissions(
    monkeypatch: pytest.MonkeyPatch,
    role_name: str,
) -> None:
    await _set_current_roles(monkeypatch, [role_name], ['users:*', 'stats:*', 'tickets:*'])

    with pytest.raises(HTTPException) as exc:
        await cabinet_dependencies.ensure_mobile_admin_access(_Db(), _User(), 'users:read')

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert 'Superadmin, Admin, or Moderator' in str(exc.value.detail)


@pytest.mark.asyncio
async def test_mobile_admin_no_role_user_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    await _set_current_roles(monkeypatch, [], ['*:*'])

    with pytest.raises(HTTPException) as exc:
        await cabinet_dependencies.ensure_mobile_admin_access(_Db(), _User(), 'users:read')

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_mobile_admin_downgraded_user_uses_current_database_roles_not_stale_token_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _User()
    user.stale_token_roles = ['Admin']
    user.stale_token_permissions = ['users:*']
    await _set_current_roles(monkeypatch, ['Support'], ['users:*'])

    with pytest.raises(HTTPException) as exc:
        await cabinet_dependencies.ensure_mobile_admin_access(_Db(), user, 'users:read')

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_mobile_admin_permission_is_still_enforced_for_allowed_role(monkeypatch: pytest.MonkeyPatch) -> None:
    await _set_current_roles(monkeypatch, ['Moderator'], ['tickets:*'])

    with pytest.raises(HTTPException) as exc:
        await cabinet_dependencies.ensure_mobile_admin_access(_Db(), _User(), 'settings:edit')

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert 'Permission not granted by any role' in str(exc.value.detail)


@pytest.mark.asyncio
async def test_mobile_admin_uses_permission_service_for_abac_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    await _set_current_roles(monkeypatch, ['Admin'], ['settings:*'])
    calls: list[dict] = []

    async def fake_check_permission(_db, _user, required_permission: str, *, ip_address=None):
        assert required_permission == 'settings:edit'
        return False, 'Denied by policy: office-hours'

    async def fake_log_action(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr('app.services.permission_service.PermissionService.check_permission', fake_check_permission)
    monkeypatch.setattr('app.services.permission_service.PermissionService.log_action', fake_log_action)
    db = _Db()

    with pytest.raises(HTTPException) as exc:
        await cabinet_dependencies.ensure_mobile_admin_access(db, _User(), 'settings:edit')

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert 'Denied by policy: office-hours' in str(exc.value.detail)
    assert calls[-1]['status'] == 'denied'
    db.commit.assert_awaited()


def test_mobile_facade_does_not_import_or_require_legacy_root_web_api_auth() -> None:
    source = inspect.getsource(admin_mobile)

    assert 'require_api_token' not in source
    assert 'X-API-Key' not in source
    assert 'WEB_API_DEFAULT_TOKEN' not in source
    assert 'query_params.get' not in source


def test_mobile_facade_wraps_authenticated_mobile_consumed_routes_with_mobile_guard() -> None:
    source = inspect.getsource(admin_mobile)

    expected = [
        "require_mobile_admin_permission('tickets:read')",
        "require_mobile_admin_permission('tickets:reply')",
        "require_mobile_admin_permission('tickets:close')",
        "require_mobile_admin_permission('users:read')",
        "require_mobile_admin_permission('users:balance')",
        "require_mobile_admin_permission('users:subscription')",
        "require_mobile_admin_permission('stats:read')",
        "require_mobile_admin_permission('settings:edit')",
    ]
    for needle in expected:
        assert needle in source


def test_mobile_subscription_fixture_shape() -> None:
    now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    subscription = SimpleNamespace(
        id=7,
        user_id=42,
        status='active',
        actual_status='active',
        is_trial=False,
        start_date=now,
        end_date=now,
        traffic_limit_gb=100,
        traffic_used_gb=12.5,
        device_limit=3,
        autopay_enabled=False,
        autopay_days_before=None,
        subscription_url='https://example.test/sub/abc',
        subscription_crypto_link=None,
        connected_squads=['squad-a'],
        created_at=now,
        updated_at=now,
    )

    response = admin_mobile._serialize_subscription(subscription)

    assert response.contract_version == CONTRACT_VERSION
    assert response.required_role_names == ['Superadmin', 'Admin', 'Moderator']
    assert response.id == 7
    assert response.subscription_url == 'https://example.test/sub/abc'


def test_month_bounds_are_utc_and_exclusive() -> None:
    start, end = admin_mobile._month_bounds(2026, 12)

    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_auth_and_refresh_responses_expose_refresh_lifetime() -> None:
    user = UserResponse(id=42, created_at=datetime(2026, 7, 3, tzinfo=UTC))

    auth = AuthResponse(
        access_token='access',
        refresh_token='refresh',
        expires_in=900,
        refresh_expires_in=2_592_000,
        user=user,
    )
    refresh = TokenResponse(
        access_token='new-access',
        refresh_token='refresh',
        expires_in=900,
        refresh_expires_in=2_500_000,
    )

    assert auth.refresh_expires_in == 2_592_000
    assert refresh.refresh_expires_in == 2_500_000
