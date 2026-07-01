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


def test_mutating_callbacks_are_gated():
    # Subscription-mutating actions live in users.py but own the 'subscriptions'
    # section. admin_sub_ must NOT collide with admin_submenu_ (navigation).
    assert resolve_admin_section('admin_sub_delete_5') == 'subscriptions'
    assert resolve_admin_section('admin_sub_grant_5') == 'subscriptions'
    assert resolve_admin_section('admin_sub_extend_5') == 'subscriptions'
    assert resolve_admin_section('admin_sub_activate_5') == 'subscriptions'
    assert resolve_admin_section('admin_buy_sub_execute_5_30_100') == 'subscriptions'
    assert resolve_admin_section('admin_send_expiry_reminders') == 'subscriptions'
    # admin_sub_ must not swallow navigation submenus
    assert resolve_admin_section('admin_submenu_settings') is None
    # payments
    assert resolve_admin_section('admin_txn_refund_5') == 'payments'
    assert resolve_admin_section('admin_stxn_5') == 'payments'
    assert resolve_admin_section('admin_withdrawal_approve_5') == 'payments'
    assert resolve_admin_section('admin_nalogo_retry:abc') == 'payments'
    # servers
    assert resolve_admin_section('admin_squad_manage_5') == 'servers'
    assert resolve_admin_section('admin_node_manage_5') == 'servers'
    assert resolve_admin_section('admin_restart_all_nodes') == 'servers'
    assert resolve_admin_section('admin_migration_confirm') == 'servers'
    # support (all *_ticket_ actions + ticket housekeeping)
    assert resolve_admin_section('admin_close_ticket_5') == 'support'
    assert resolve_admin_section('admin_reply_ticket_5') == 'support'
    assert resolve_admin_section('admin_block_user_ticket_5') == 'support'
    assert resolve_admin_section('admin_view_ticket_5') == 'support'
    # broadcasts
    assert resolve_admin_section('admin_confirm_broadcast') == 'broadcasts'
    # settings (config toggles under the settings submenu)
    assert resolve_admin_section('admin_edit_rules') == 'settings'
    assert resolve_admin_section('admin_save_rules') == 'settings'
    assert resolve_admin_section('admin_clear_rules') == 'settings'
    assert resolve_admin_section('admin_freeze_toggle') == 'settings'
    assert resolve_admin_section('admin_birthday_toggle') == 'settings'
    assert resolve_admin_section('admin_traffic_toggle_fast') == 'settings'
    # mon_settings stays settings, generic mon_ stays analytics (order preserved)
    assert resolve_admin_section('admin_mon_settings') == 'settings'
    assert resolve_admin_section('admin_mon_start') == 'analytics'
    # A support-only admin must NOT reach these non-support sections:
    for cb in ('admin_sub_delete_5', 'admin_txn_refund_5', 'admin_squad_manage_5',
               'admin_confirm_broadcast', 'admin_edit_rules'):
        assert resolve_admin_section(cb) != 'support'


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
