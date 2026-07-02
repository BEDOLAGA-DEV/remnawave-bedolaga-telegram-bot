"""filter_admin_keyboard drops buttons the admin lacks; superadmin sees all."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.keyboards.admin import filter_admin_keyboard, get_admin_main_keyboard


def _all_callbacks(markup: InlineKeyboardMarkup) -> set[str]:
    return {b.callback_data for row in markup.inline_keyboard for b in row}


def test_superadmin_sees_everything():
    kb = get_admin_main_keyboard('ru')
    filtered = filter_admin_keyboard(kb, permissions=None, is_super=True)
    assert _all_callbacks(filtered) == _all_callbacks(kb)


def test_section_admin_sees_only_permitted_direct_buttons():
    kb = get_admin_main_keyboard('ru')
    filtered = filter_admin_keyboard(kb, permissions=['servers'], is_super=False)
    cbs = _all_callbacks(filtered)

    # direct section button they have
    assert 'admin_servers' in cbs
    # direct section button they lack
    assert 'admin_tariffs' not in cbs
    # role management is super-only, hidden for section admins
    assert 'admin_bot_roles' not in cbs
    # submenu buttons are now gated by whether the admin has any child section:
    # settings submenu has servers children (admin_remnawave/admin_remna_config)
    assert 'admin_submenu_settings' in cbs
    # users submenu's children (users/subscriptions) do not include 'servers'
    assert 'admin_submenu_users' not in cbs


def test_empty_rows_are_dropped():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Servers', callback_data='admin_servers')],
        [InlineKeyboardButton(text='Tariffs', callback_data='admin_tariffs')],
    ])
    filtered = filter_admin_keyboard(kb, permissions=['servers'], is_super=False)
    assert len(filtered.inline_keyboard) == 1
    assert filtered.inline_keyboard[0][0].callback_data == 'admin_servers'


def test_submenu_hidden_when_no_child_sections():
    from app.keyboards.admin import get_admin_main_keyboard, filter_admin_keyboard
    kb = get_admin_main_keyboard('ru')
    filtered = filter_admin_keyboard(kb, permissions=['servers'], is_super=False)
    cbs = {b.callback_data for row in filtered.inline_keyboard for b in row}
    # users submenu's children (users/subscriptions) do not include 'servers'
    assert 'admin_submenu_users' not in cbs
    # settings submenu contains servers children (admin_remnawave/admin_remna_config)
    assert 'admin_submenu_settings' in cbs  # settings submenu contains servers children


def test_submenu_shown_when_has_a_child_section():
    from app.keyboards.admin import get_admin_main_keyboard, filter_admin_keyboard
    kb = get_admin_main_keyboard('ru')
    filtered = filter_admin_keyboard(kb, permissions=['users'], is_super=False)
    cbs = {b.callback_data for row in filtered.inline_keyboard for b in row}
    assert 'admin_submenu_users' in cbs
