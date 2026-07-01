from types import SimpleNamespace


def _server(is_default=False):
    return SimpleNamespace(
        id=7,
        squad_uuid='u7',
        display_name='Main',
        original_name=None,
        is_available=True,
        is_trial_eligible=False,
        is_default=is_default,
        price_kopeks=0,
        price_rubles=0.0,
        country_code=None,
        max_users=None,
        current_users=0,
        allowed_promo_groups=[],
        description=None,
    )


def test_edit_view_has_set_default_button():
    from app.handlers.admin.servers import _build_server_edit_view

    _text, kb = _build_server_edit_view(_server(is_default=False))
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert 'admin_server_set_default_7' in cbs


def test_edit_view_shows_main_badge_only_for_default():
    from app.handlers.admin.servers import _build_server_edit_view

    default_text, _kb = _build_server_edit_view(_server(is_default=True))
    assert '⭐ Основной' in default_text

    normal_text, _kb = _build_server_edit_view(_server(is_default=False))
    assert '⭐ Основной' not in normal_text
    assert '⚪️ Обычный' in normal_text
