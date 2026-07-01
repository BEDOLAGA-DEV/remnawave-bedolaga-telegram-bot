"""nz!_/spromo_ ADMIN actions gate; nz!_ USER actions never gate."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery

from app.config import settings
from app.middlewares.admin_permission import (
    AdminPermissionMiddleware,
    resolve_admin_section as r,
)

ADMIN_SAMPLES = {
    'nz!_broadcast_all': 'broadcasts',
    'nz!_criteria_today': 'broadcasts',
    'nz!_sync_all_users': 'servers',
    'nz!_node_restart_5': 'servers',
    'nz!_squad_delete_3': 'servers',
    'nz!_promo_delete_7': 'promos',
    'nz!_promo_group_manage_2': 'promos',
    'nz!_promo_offer_edit_1': 'promos',
    'nz!_poll_create': 'promos',
    'nz!_maintenance_panel': 'settings',
    'nz!_welcome_text_panel': 'settings',
    'nz!_reqch:list': 'settings',
    'nz!_user_messages_panel': 'broadcasts',
    'spromo_view:3': 'offers',
}
USER_SAMPLES = [
    'nz!_menu_buy', 'nz!_trial_activate', 'nz!_rules_accept', 'nz!_back_to_menu',
    'nz!_current_page', 'nz!_noop', 'nz!_language_select:ru', 'nz!_subscription_connect',
    'nz!_promo_sub', 'nz!_menu_promocode', 'nz!_poll_answer_1', 'nz!_period_30',
    'nz!_my_tickets', 'nz!_incy_open', 'nz!_bio_reward_open',
]


def test_admin_nz_callbacks_gate():
    for cb, sect in ADMIN_SAMPLES.items():
        assert r(cb) == sect, f'{cb} -> {r(cb)} (want {sect})'


def test_user_nz_callbacks_never_gate():
    for cb in USER_SAMPLES:
        assert r(cb) is None, f'{cb} wrongly resolved to {r(cb)}'


# --- Step 7: middleware trace tests ---

def _event(data: str):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=222)
    cb.answer = AsyncMock()
    return cb


def _data():
    return {
        'db': MagicMock(),
        'db_user': SimpleNamespace(id=1, telegram_id=222, language='ru'),
    }


@pytest.fixture
def not_superadmin(monkeypatch):
    monkeypatch.setattr(type(settings), 'get_admin_ids', lambda self: [999])


async def test_plain_user_nz_callback_runs(not_superadmin):
    """A normal user clicking nz!_menu_buy must reach the handler (not denied)."""
    mw = AdminPermissionMiddleware()
    handler = AsyncMock(return_value='ran')
    event = _event('nz!_menu_buy')

    with patch('app.database.crud.bot_role.BotRoleCRUD.get_bot_role',
               new=AsyncMock(return_value=None)):
        result = await mw(handler, event, _data())

    handler.assert_awaited_once()
    event.answer.assert_not_awaited()
    assert result == 'ran'


async def test_section_admin_denied_on_nz_admin_action(not_superadmin):
    """A support-only admin clicking nz!_sync_all_users (servers) must be denied."""
    mw = AdminPermissionMiddleware()
    role = SimpleNamespace(permissions=['support'])
    handler = AsyncMock(return_value='ran')
    event = _event('nz!_sync_all_users')

    with patch('app.database.crud.bot_role.BotRoleCRUD.get_bot_role',
               new=AsyncMock(return_value=role)):
        result = await mw(handler, event, _data())

    handler.assert_not_awaited()
    event.answer.assert_awaited()
    assert result is None
