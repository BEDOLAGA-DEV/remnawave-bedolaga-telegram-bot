from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.admin.tariff_custom_traffic as custom_mod
from app.handlers.admin.tariffs import format_tariff_info, get_tariff_view_keyboard


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _tariff(**overrides):
    values = {
        'id': 7,
        'name': 'Whitelist <test>',
        'description': None,
        'is_active': True,
        'is_trial_available': False,
        'trial_duration_days': None,
        'traffic_limit_gb': 100,
        'device_limit': 1,
        'max_device_limit': None,
        'device_price_kopeks': None,
        'tier_level': 1,
        'display_order': 0,
        'period_prices': {'30': 0},
        'allowed_squads': [],
        'allowed_promo_groups': [],
        'traffic_topup_enabled': False,
        'traffic_reset_mode': None,
        'is_daily': False,
        'daily_price_kopeks': 0,
        'custom_traffic_enabled': False,
        'traffic_price_per_gb_kopeks': 0,
        'min_traffic_gb': 1,
        'max_traffic_gb': 1000,
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


def _message(text: str | None):
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


def _state(tariff_id: int = 7):
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': tariff_id, 'language': 'ru'})
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    state.clear = AsyncMock()
    return state


def _callbacks(keyboard) -> list[str]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]


def test_tariff_card_renders_custom_traffic_status_and_navigation() -> None:
    tariff = _tariff(
        custom_traffic_enabled=True,
        traffic_price_per_gb_kopeks=200,
        min_traffic_gb=5,
        max_traffic_gb=100,
    )

    rendered = format_tariff_info(tariff, 'ru')
    callbacks = _callbacks(get_tariff_view_keyboard(tariff, 'ru'))

    assert '<b>Произвольный трафик:</b>' in rendered
    assert '✅ Включено' in rendered
    assert 'Цена за 1 ГБ: 2 ₽' in rendered
    assert 'Минимальный объём: 5 ГБ' in rendered
    assert 'Максимальный объём: 100 ГБ' in rendered
    assert 'admin_tariff_edit_custom_traffic:7' in callbacks


def test_custom_traffic_screen_uses_neutral_value_for_unset_price() -> None:
    tariff = _tariff(traffic_price_per_gb_kopeks=0)

    rendered = custom_mod.render_custom_traffic_settings(tariff)

    assert 'Цена за 1 ГБ: <b>Не задано</b>' in rendered
    assert 'Минимальный объём: <b>1 ГБ</b>' in rendered
    assert 'Максимальный объём: <b>1000 ГБ</b>' in rendered
    assert 'Whitelist &lt;test&gt;' in rendered


async def test_enable_persists_only_enabled_flag_for_valid_settings(monkeypatch) -> None:
    tariff = _tariff(
        custom_traffic_enabled=False,
        traffic_price_per_gb_kopeks=200,
        min_traffic_gb=5,
        max_traffic_gb=100,
    )
    callback = _callback('admin_tariff_toggle_custom_traffic:7')
    updates = []

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))

    async def fake_update(db, target, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    monkeypatch.setattr(custom_mod, 'update_tariff', fake_update)

    await _unwrap(custom_mod.toggle_custom_traffic)(callback, SimpleNamespace(language='ru'), MagicMock())

    assert updates == [{'custom_traffic_enabled': True}]
    callback.message.edit_text.assert_awaited_once()
    assert callback.answer.await_args.args[0] == 'Произвольный трафик включён'


async def test_enable_rejects_invalid_settings_without_write(monkeypatch) -> None:
    tariff = _tariff(
        custom_traffic_enabled=False,
        traffic_price_per_gb_kopeks=0,
        min_traffic_gb=100,
        max_traffic_gb=5,
    )
    callback = _callback('admin_tariff_toggle_custom_traffic:7')
    update = AsyncMock()

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(custom_mod, 'update_tariff', update)

    await _unwrap(custom_mod.toggle_custom_traffic)(callback, SimpleNamespace(language='ru'), MagicMock())

    update.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    alert = callback.answer.await_args.args[0]
    assert 'цена за 1 ГБ должна быть больше нуля' in alert
    assert 'максимальный объём не может быть меньше минимального' in alert


async def test_disable_preserves_price_and_bounds(monkeypatch) -> None:
    tariff = _tariff(
        custom_traffic_enabled=True,
        traffic_price_per_gb_kopeks=200,
        min_traffic_gb=5,
        max_traffic_gb=100,
    )
    callback = _callback('admin_tariff_toggle_custom_traffic:7')
    updates = []

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))

    async def fake_update(db, target, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    monkeypatch.setattr(custom_mod, 'update_tariff', fake_update)

    await _unwrap(custom_mod.toggle_custom_traffic)(callback, SimpleNamespace(language='ru'), MagicMock())

    assert updates == [{'custom_traffic_enabled': False}]
    assert tariff.traffic_price_per_gb_kopeks == 200
    assert tariff.min_traffic_gb == 5
    assert tariff.max_traffic_gb == 100


async def test_price_input_converts_rubles_exactly_and_updates_only_price(monkeypatch) -> None:
    tariff = _tariff()
    message = _message('2,50')
    state = _state()
    updates = []

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))

    async def fake_update(db, target, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    monkeypatch.setattr(custom_mod, 'update_tariff', fake_update)

    await _unwrap(custom_mod.process_custom_traffic_price_input)(
        message,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    assert updates == [{'traffic_price_per_gb_kopeks': 250}]
    state.clear.assert_awaited_once()
    assert '2.50 ₽' in message.answer.await_args.args[0]


async def test_invalid_price_keeps_fsm_active_and_does_not_write(monkeypatch) -> None:
    tariff = _tariff()
    message = _message('1.001')
    state = _state()
    update = AsyncMock()

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(custom_mod, 'update_tariff', update)

    await _unwrap(custom_mod.process_custom_traffic_price_input)(
        message,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert 'Некорректная цена' in message.answer.await_args.args[0]


async def test_minimum_above_current_maximum_is_rejected(monkeypatch) -> None:
    tariff = _tariff(min_traffic_gb=5, max_traffic_gb=100)
    message = _message('101')
    state = _state()
    update = AsyncMock()

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(custom_mod, 'update_tariff', update)

    await _unwrap(custom_mod.process_custom_traffic_min_input)(
        message,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert 'не может быть больше текущего максимума' in message.answer.await_args.args[0]


async def test_maximum_below_current_minimum_is_rejected(monkeypatch) -> None:
    tariff = _tariff(min_traffic_gb=5, max_traffic_gb=100)
    message = _message('4')
    state = _state()
    update = AsyncMock()

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(custom_mod, 'update_tariff', update)

    await _unwrap(custom_mod.process_custom_traffic_max_input)(
        message,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert 'не может быть меньше текущего минимума' in message.answer.await_args.args[0]


async def test_missing_tariff_is_handled_without_update(monkeypatch) -> None:
    callback = _callback('admin_tariff_edit_custom_traffic:999')
    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=None))

    state = _state(999)
    await _unwrap(custom_mod.show_custom_traffic_settings)(
        callback,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    state.clear.assert_awaited_once()
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.args[0] == 'Тариф не найден'
    assert callback.answer.await_args.kwargs['show_alert'] is True


def test_custom_traffic_screen_renders_all_unset_values_and_back_navigation() -> None:
    tariff = _tariff(
        traffic_price_per_gb_kopeks=None,
        min_traffic_gb=None,
        max_traffic_gb=None,
    )

    rendered = custom_mod.render_custom_traffic_settings(tariff)
    callbacks = _callbacks(custom_mod.get_custom_traffic_keyboard(tariff, 'ru'))

    assert rendered.count('Не задано') == 3
    assert 'admin_tariff_view:7' in callbacks
    assert 'admin_tariff_toggle_custom_traffic:7' in callbacks


def test_existing_topup_control_remains_independent() -> None:
    tariff = _tariff()
    callbacks = _callbacks(get_tariff_view_keyboard(tariff, 'ru'))

    assert 'admin_tariff_edit_custom_traffic:7' in callbacks
    assert 'admin_tariff_edit_traffic_topup:7' in callbacks


async def test_show_settings_clears_field_edit_state(monkeypatch) -> None:
    tariff = _tariff()
    callback = _callback('admin_tariff_edit_custom_traffic:7')
    state = _state()

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))

    await _unwrap(custom_mod.show_custom_traffic_settings)(
        callback,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    state.clear.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once_with()


async def test_minimum_success_updates_only_minimum(monkeypatch) -> None:
    tariff = _tariff(min_traffic_gb=1, max_traffic_gb=100)
    message = _message('5')
    state = _state()
    updates = []

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))

    async def fake_update(db, target, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    monkeypatch.setattr(custom_mod, 'update_tariff', fake_update)

    await _unwrap(custom_mod.process_custom_traffic_min_input)(
        message,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    assert updates == [{'min_traffic_gb': 5}]
    state.clear.assert_awaited_once()


async def test_maximum_success_updates_only_maximum(monkeypatch) -> None:
    tariff = _tariff(min_traffic_gb=5, max_traffic_gb=1000)
    message = _message('100')
    state = _state()
    updates = []

    monkeypatch.setattr(custom_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))

    async def fake_update(db, target, **kwargs):
        updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    monkeypatch.setattr(custom_mod, 'update_tariff', fake_update)

    await _unwrap(custom_mod.process_custom_traffic_max_input)(
        message,
        SimpleNamespace(language='ru'),
        MagicMock(),
        state,
    )

    assert updates == [{'max_traffic_gb': 100}]
    state.clear.assert_awaited_once()
