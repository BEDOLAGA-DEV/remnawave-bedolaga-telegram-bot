"""get_admin_pagination_keyboard must emit callbacks the admin list handlers listen for."""
from app.keyboards.admin import get_admin_pagination_keyboard


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_page_callbacks_have_no_nz_prefix():
    kb = get_admin_pagination_keyboard(
        current_page=2, total_pages=5, callback_prefix='admin_campaigns_list',
        back_callback='admin_campaigns', language='ru',
    )
    cbs = _cbs(kb)
    assert 'admin_campaigns_list_page_1' in cbs   # prev
    assert 'admin_campaigns_list_page_3' in cbs   # next
    assert not any(c.startswith('nz!_admin_campaigns_list_page_') for c in cbs)
