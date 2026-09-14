"""Экран «Ещё настройки» телеграм-редактора: тег панели, внешний сквад, Lava, подарки,
докупка, порядок — то, что раньше правилось только в кабинете."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.admin.tariff_panel_settings as mod
from app.handlers.admin.tariffs import format_tariff_info, get_tariff_view_keyboard


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _tariff(**overrides):
    values = {
        'id': 7,
        'name': 'Gold <t>',
        'description': None,
        'is_active': True,
        'is_trial_available': False,
        'trial_duration_days': None,
        'traffic_limit_gb': 100,
        'device_limit': 1,
        'max_device_limit': None,
        'device_price_kopeks': None,
        'tier_level': 1,
        'display_order': 2,
        'period_prices': {'30': 100},
        'highlight_period_days': None,
        'allowed_squads': [],
        'allowed_promo_groups': [],
        'server_traffic_limits': {},
        'traffic_topup_enabled': False,
        'allow_traffic_topup': True,
        'traffic_reset_mode': None,
        'is_daily': False,
        'daily_price_kopeks': 0,
        'custom_traffic_enabled': False,
        'traffic_price_per_gb_kopeks': 0,
        'min_traffic_gb': 1,
        'max_traffic_gb': 1000,
        'custom_days_enabled': False,
        'price_per_day_kopeks': 0,
        'min_days': 1,
        'max_days': 365,
        'panel_tag': None,
        'external_squad_uuid': None,
        'lava_product_id': None,
        'show_in_gift': True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _callback(data: str):
    callback = MagicMock()
    callback.data = data
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


def _message(text):
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


def _state(**data):
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': 7, 'language': 'ru', **data})
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    return state


def _callbacks(keyboard) -> list[str]:
    return [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]


def _recording_update(monkeypatch, updates: list):
    async def fake_update(db, target, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    monkeypatch.setattr(mod, 'update_tariff', fake_update)


# ---- карточка и экран ----


def test_tariff_card_shows_panel_block_and_entry_point() -> None:
    tariff = _tariff(panel_tag='GOLD', lava_product_id='prod-1', external_squad_uuid='ext-1', show_in_gift=False)

    rendered = format_tariff_info(tariff, 'ru')
    callbacks = _callbacks(get_tariff_view_keyboard(tariff, 'ru'))

    assert 'Тег панели' in rendered and 'GOLD' in rendered
    assert 'Внешний сквад' in rendered and 'ext-1' in rendered
    assert 'Lava' in rendered and 'prod-1' in rendered
    assert 'В подарках' in rendered
    assert 'admin_tariff_edit_more:7' in callbacks


def test_settings_screen_lists_all_actions_and_current_values() -> None:
    tariff = _tariff(panel_tag='GOLD', allow_traffic_topup=False)

    rendered = mod.render_panel_settings(tariff)
    callbacks = _callbacks(mod.get_panel_settings_keyboard(tariff, 'ru'))

    assert 'Gold &lt;t&gt;' in rendered
    assert 'GOLD' in rendered
    for expected in (
        'admin_tariff_edit_panel_tag:7',
        'admin_tariff_edit_ext_squad:7',
        'admin_tariff_edit_lava:7',
        'admin_tariff_edit_order:7',
        'admin_tariff_toggle_gift:7',
        'admin_tariff_toggle_allow_topup:7',
        'admin_tariff_view:7',
    ):
        assert expected in callbacks


# ---- переключатели ----


async def test_toggle_gift_flips_flag(monkeypatch) -> None:
    tariff = _tariff(show_in_gift=True)
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.toggle_show_in_gift)(
        _callback('admin_tariff_toggle_gift:7'), SimpleNamespace(language='ru'), MagicMock()
    )

    assert updates == [{'show_in_gift': False}]


async def test_toggle_allow_topup_flips_flag(monkeypatch) -> None:
    tariff = _tariff(allow_traffic_topup=False)
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.toggle_allow_traffic_topup)(
        _callback('admin_tariff_toggle_allow_topup:7'), SimpleNamespace(language='ru'), MagicMock()
    )

    assert updates == [{'allow_traffic_topup': True}]


# ---- тег панели ----


async def test_panel_tag_input_is_normalized_before_write(monkeypatch) -> None:
    tariff = _tariff()
    updates: list = []
    state = _state()
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)

    message = _message(' gold_1 ')
    await _unwrap(mod.process_panel_tag_input)(message, SimpleNamespace(language='ru'), MagicMock(), state)

    assert updates == [{'panel_tag': 'GOLD_1'}]
    state.clear.assert_awaited_once()
    assert 'GOLD_1' in message.answer.await_args.args[0]


async def test_panel_tag_dash_clears(monkeypatch) -> None:
    tariff = _tariff(panel_tag='GOLD')
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.process_panel_tag_input)(_message('-'), SimpleNamespace(language='ru'), MagicMock(), _state())

    assert updates == [{'panel_tag': None}]


async def test_invalid_panel_tag_keeps_state_and_does_not_write(monkeypatch) -> None:
    tariff = _tariff()
    update = AsyncMock()
    state = _state()
    message = _message('gold-1')
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'update_tariff', update)

    await _unwrap(mod.process_panel_tag_input)(message, SimpleNamespace(language='ru'), MagicMock(), state)

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert 'латинские буквы' in message.answer.await_args.args[0]


# ---- Lava и порядок ----


async def test_lava_input_and_dash_clear(monkeypatch) -> None:
    tariff = _tariff()
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.process_lava_product_input)(
        _message(' prod-1 '), SimpleNamespace(language='ru'), MagicMock(), _state()
    )
    await _unwrap(mod.process_lava_product_input)(_message('-'), SimpleNamespace(language='ru'), MagicMock(), _state())

    assert updates == [{'lava_product_id': 'prod-1'}, {'lava_product_id': ''}]


async def test_display_order_accepts_non_negative_int_only(monkeypatch) -> None:
    tariff = _tariff()
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)
    bad = _message('-1')
    state = _state()

    await _unwrap(mod.process_display_order_input)(bad, SimpleNamespace(language='ru'), MagicMock(), state)
    await _unwrap(mod.process_display_order_input)(_message('3'), SimpleNamespace(language='ru'), MagicMock(), _state())

    assert updates == [{'display_order': 3}]
    state.clear.assert_not_awaited()
    assert 'целое число' in bad.answer.await_args.args[0]


# ---- внешний сквад ----


async def test_external_squad_list_marks_current_and_offers_none(monkeypatch) -> None:
    tariff = _tariff(external_squad_uuid='ext-1')
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        mod,
        'load_external_squads',
        AsyncMock(
            return_value=[SimpleNamespace(uuid='ext-1', name='Alpha'), SimpleNamespace(uuid='ext-2', name='Beta')]
        ),
    )
    callback = _callback('admin_tariff_edit_ext_squad:7')

    await _unwrap(mod.start_edit_external_squad)(callback, SimpleNamespace(language='ru'), MagicMock(), _state())

    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup']
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    callbacks = _callbacks(keyboard)
    assert any('✅' in label and 'Alpha' in label for label in labels)
    assert 'admin_tariff_set_ext_squad:7:ext-2' in callbacks
    assert 'admin_tariff_set_ext_squad:7:-' in callbacks


async def test_set_external_squad_writes_and_schedules_sync(monkeypatch) -> None:
    tariff = _tariff(external_squad_uuid='ext-1')
    updates: list = []
    scheduled: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)
    monkeypatch.setattr(
        mod, 'schedule_tariff_squad_sync', lambda tariff_id, admin_id: scheduled.append((tariff_id, admin_id))
    )
    monkeypatch.setattr(mod, 'load_external_squads', AsyncMock(return_value=[]))

    await _unwrap(mod.set_external_squad)(
        _callback('admin_tariff_set_ext_squad:7:-'), SimpleNamespace(language='ru', id=1), MagicMock()
    )

    assert updates == [{'external_squad_uuid': None}]
    assert scheduled == [(7, 1)]


async def test_set_same_external_squad_does_not_resync(monkeypatch) -> None:
    tariff = _tariff(external_squad_uuid='ext-1')
    updates: list = []
    scheduled: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)
    monkeypatch.setattr(
        mod, 'schedule_tariff_squad_sync', lambda tariff_id, admin_id: scheduled.append((tariff_id, admin_id))
    )
    monkeypatch.setattr(mod, 'load_external_squads', AsyncMock(return_value=[]))

    await _unwrap(mod.set_external_squad)(
        _callback('admin_tariff_set_ext_squad:7:ext-1'), SimpleNamespace(language='ru', id=1), MagicMock()
    )

    assert updates == []
    assert scheduled == []
