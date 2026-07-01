def test_manage_protocols_keyboard_marks_selection_and_callbacks():
    from app.keyboards.inline import get_manage_protocols_keyboard

    pool = [{'uuid': 'a', 'name': 'Main'}, {'uuid': 'b', 'name': 'Extra'}]
    kb = get_manage_protocols_keyboard(pool, ['a'], 'ru')

    texts = [b.text for row in kb.inline_keyboard for b in row]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]

    assert any(t.startswith('✅') and 'Main' in t for t in texts)
    assert any(t.startswith('⚪') and 'Extra' in t for t in texts)
    assert 'nz!_protocol_toggle_a' in cbs
    assert 'nz!_protocol_toggle_b' in cbs
    assert 'nz!_protocols_apply' in cbs
    assert 'nz!_subscription_settings' in cbs
