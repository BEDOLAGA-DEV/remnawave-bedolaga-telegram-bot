from app.keyboards.inline import get_info_menu_keyboard


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_rules_button_shown_by_default():
    markup = get_info_menu_keyboard()
    assert 'menu_rules' in _callbacks(markup)


def test_rules_button_hidden_when_disabled():
    markup = get_info_menu_keyboard(show_rules=False)
    assert 'menu_rules' not in _callbacks(markup)


def test_custom_page_buttons_added():
    markup = get_info_menu_keyboard(custom_pages=[(5, '📄 О сервисе'), (7, 'Гайд')])
    callbacks = _callbacks(markup)
    assert 'info_page:5:1' in callbacks
    assert 'info_page:7:1' in callbacks


def test_no_custom_buttons_without_pages():
    markup = get_info_menu_keyboard()
    assert not [cb for cb in _callbacks(markup) if cb.startswith('info_page:')]
