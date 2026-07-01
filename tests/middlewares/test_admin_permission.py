"""AdminPermissionMiddleware gates admin_* callbacks by BotAdminRole sections."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery

from app.config import settings
from app.middlewares.admin_permission import (
    AdminPermissionMiddleware,
    resolve_admin_section,
)


def test_resolve_admin_section():
    assert resolve_admin_section('admin_users') == 'users'
    assert resolve_admin_section('admin_user_balance_5') == 'users'
    assert resolve_admin_section('admin_tickets') == 'support'
    assert resolve_admin_section('admin_bot_roles') == 'settings'
    assert resolve_admin_section('bot_role_save:5') is None   # not an admin_ callback
    assert resolve_admin_section('admin_totally_unknown') is None


def test_map_covers_known_gaps():
    # These prefixes were unmapped and leaked to "any admin" before the audit.
    assert resolve_admin_section('admin_rw_nodes') == 'servers'
    assert resolve_admin_section('admin_subs_list') == 'subscriptions'
    assert resolve_admin_section('admin_stats_users') == 'analytics'
    assert resolve_admin_section('admin_mon_start') == 'analytics'
    assert resolve_admin_section('admin_mon_settings') == 'settings'  # more specific wins
    assert resolve_admin_section('admin_msg_all') == 'broadcasts'
    assert resolve_admin_section('admin_campaign_stats_3') == 'promos'
    assert resolve_admin_section('admin_contest_toggle_3') == 'promos'
    assert resolve_admin_section('admin_daily_toggle_3') == 'promos'
    assert resolve_admin_section('admin_wl_analytics') == 'analytics'
    assert resolve_admin_section('admin_mass_delete_start') == 'users'


def _event(data: str):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=222)
    cb.answer = AsyncMock()
    return cb


def _data(permissions):
    role = SimpleNamespace(permissions=permissions)
    return {
        'db': MagicMock(),
        'db_user': SimpleNamespace(id=1, telegram_id=222, language='ru'),
    }, role


@pytest.fixture
def not_superadmin(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [111])


async def test_denies_missing_section(not_superadmin):
    mw = AdminPermissionMiddleware()
    data, role = _data(['support'])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_users')  # needs 'users', role only has 'support'

    with patch('app.database.crud.bot_role.BotRoleCRUD.get_bot_role',
               new=AsyncMock(return_value=role)):
        result = await mw(handler, event, data)

    handler.assert_not_awaited()
    event.answer.assert_awaited()
    assert result is None


async def test_allows_present_section(not_superadmin):
    mw = AdminPermissionMiddleware()
    data, role = _data(['users'])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_users')

    with patch('app.database.crud.bot_role.BotRoleCRUD.get_bot_role',
               new=AsyncMock(return_value=role)):
        result = await mw(handler, event, data)

    handler.assert_awaited_once()
    assert result == 'ran'


async def test_superadmin_bypass(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [222])  # event user is super
    mw = AdminPermissionMiddleware()
    data, _ = _data([])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_users')

    result = await mw(handler, event, data)
    assert result == 'ran'


async def test_navigation_always_allowed(not_superadmin):
    mw = AdminPermissionMiddleware()
    data, _ = _data([])
    handler = AsyncMock(return_value='ran')
    event = _event('admin_panel')

    result = await mw(handler, event, data)
    assert result == 'ran'
