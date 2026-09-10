"""Лимиты трафика по серверам тарифа в телеграм-редакторе (раньше только кабинет)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.admin.tariff_server_limits as mod
from app.handlers.admin.tariffs import format_tariff_info, get_tariff_view_keyboard


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


SQUADS = [
    SimpleNamespace(squad_uuid='sq-1', display_name='Amsterdam'),
    SimpleNamespace(squad_uuid='sq-2', display_name='Berlin'),
]


def _tariff(**overrides):
    values = {
        'id': 7,
        'name': 'Servers',
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
        'period_prices': {'30': 100},
        'highlight_period_days': None,
        'allowed_squads': ['sq-1'],
        'allowed_promo_groups': [],
        'server_traffic_limits': {'sq-1': {'traffic_limit_gb': 50}},
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


def _state(squad_uuid='sq-1'):
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': 7, 'language': 'ru', 'squad_uuid': squad_uuid})
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


def test_card_summarizes_limits_and_has_entry() -> None:
    tariff = _tariff()
    rendered = format_tariff_info(tariff, 'ru')
    assert 'Лимиты по серверам' in rendered and '1' in rendered
    assert 'admin_tariff_edit_server_limits:7' in _callbacks(get_tariff_view_keyboard(tariff, 'ru'))


def test_screen_lists_allowed_squads_with_limits() -> None:
    tariff = _tariff()
    keyboard = mod.get_server_limits_keyboard(tariff, SQUADS, 'ru')
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    callbacks = _callbacks(keyboard)

    assert any('Amsterdam' in label and '50' in label for label in labels)
    assert not any('Berlin' in label for label in labels)  # не в allowed_squads
    assert 'admin_tariff_srv_limit:7:sq-1' in callbacks
    assert 'admin_tariff_view:7' in callbacks


def test_screen_lists_all_squads_when_tariff_allows_all() -> None:
    keyboard = mod.get_server_limits_keyboard(_tariff(allowed_squads=[]), SQUADS, 'ru')
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any('Berlin' in label for label in labels)


async def test_input_sets_limit_without_mutating_stored_dict(monkeypatch) -> None:
    tariff = _tariff()
    original = tariff.server_traffic_limits
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'get_all_server_squads', AsyncMock(return_value=(SQUADS, 2)))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.process_server_limit_input)(
        _message('80'), SimpleNamespace(language='ru'), MagicMock(), _state('sq-2')
    )

    assert updates == [{'server_traffic_limits': {'sq-1': {'traffic_limit_gb': 50}, 'sq-2': {'traffic_limit_gb': 80}}}]
    assert original == {'sq-1': {'traffic_limit_gb': 50}}


async def test_zero_removes_limit(monkeypatch) -> None:
    tariff = _tariff()
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'get_all_server_squads', AsyncMock(return_value=(SQUADS, 2)))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.process_server_limit_input)(
        _message('0'), SimpleNamespace(language='ru'), MagicMock(), _state('sq-1')
    )

    assert updates == [{'server_traffic_limits': {}}]


async def test_invalid_input_keeps_state(monkeypatch) -> None:
    tariff = _tariff()
    update = AsyncMock()
    state = _state()
    message = _message('много')
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'update_tariff', update)

    await _unwrap(mod.process_server_limit_input)(message, SimpleNamespace(language='ru'), MagicMock(), state)

    update.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert 'целое число' in message.answer.await_args.args[0]
