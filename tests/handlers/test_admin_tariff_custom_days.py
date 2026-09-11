"""Произвольное количество дней в телеграм-редакторе — как произвольный трафик."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.admin.tariff_custom_days as mod
from app.handlers.admin.tariffs import format_tariff_info, get_tariff_view_keyboard
from app.services.tariff_custom_days import parse_positive_days, validate_custom_days_configuration


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _tariff(**overrides):
    values = {
        'id': 7,
        'name': 'Days <t>',
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


def _state():
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'tariff_id': 7, 'language': 'ru'})
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


def test_service_parses_days_and_validates_bounds() -> None:
    assert parse_positive_days(' 30 ') == 30
    for bad in ('0', '-1', 'x', '1.5'):
        try:
            parse_positive_days(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    errors = validate_custom_days_configuration(price_per_day_kopeks=0, min_days=10, max_days=5)
    assert 'цена за 1 день должна быть больше нуля' in errors
    assert 'максимум дней не может быть меньше минимума' in errors
    assert validate_custom_days_configuration(price_per_day_kopeks=100, min_days=1, max_days=30) == ()


def test_tariff_card_shows_custom_days_block_and_entry() -> None:
    tariff = _tariff(custom_days_enabled=True, price_per_day_kopeks=1500, min_days=3, max_days=90)

    rendered = format_tariff_info(tariff, 'ru')
    callbacks = _callbacks(get_tariff_view_keyboard(tariff, 'ru'))

    assert '<b>Произвольные дни:</b>' in rendered
    assert '✅ Включено' in rendered
    assert 'Цена за 1 день: 15 ₽' in rendered
    assert 'Минимум: 3 дн.' in rendered and 'Максимум: 90 дн.' in rendered
    assert 'admin_tariff_edit_custom_days:7' in callbacks


def test_screen_lists_actions() -> None:
    tariff = _tariff()
    callbacks = _callbacks(mod.get_custom_days_keyboard(tariff, 'ru'))
    for expected in (
        'admin_tariff_toggle_custom_days:7',
        'admin_tariff_edit_custom_days_price:7',
        'admin_tariff_edit_custom_days_min:7',
        'admin_tariff_edit_custom_days_max:7',
        'admin_tariff_view:7',
    ):
        assert expected in callbacks
    assert 'Days &lt;t&gt;' in mod.render_custom_days_settings(tariff)


async def test_enable_requires_valid_settings(monkeypatch) -> None:
    tariff = _tariff(price_per_day_kopeks=0, min_days=10, max_days=5)
    update = AsyncMock()
    callback = _callback('admin_tariff_toggle_custom_days:7')
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'update_tariff', update)

    await _unwrap(mod.toggle_custom_days)(callback, SimpleNamespace(language='ru'), MagicMock())

    update.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True


async def test_enable_writes_only_flag_when_valid(monkeypatch) -> None:
    tariff = _tariff(price_per_day_kopeks=1500, min_days=3, max_days=90)
    updates: list = []
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.toggle_custom_days)(
        _callback('admin_tariff_toggle_custom_days:7'), SimpleNamespace(language='ru'), MagicMock()
    )

    assert updates == [{'custom_days_enabled': True}]


async def test_price_input_converts_rubles(monkeypatch) -> None:
    tariff = _tariff()
    updates: list = []
    state = _state()
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    _recording_update(monkeypatch, updates)

    await _unwrap(mod.process_custom_days_price_input)(
        _message('1,50'), SimpleNamespace(language='ru'), MagicMock(), state
    )

    assert updates == [{'price_per_day_kopeks': 150}]
    state.clear.assert_awaited_once()


async def test_min_above_max_is_rejected(monkeypatch) -> None:
    tariff = _tariff(min_days=1, max_days=30)
    update = AsyncMock()
    message = _message('31')
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'update_tariff', update)

    await _unwrap(mod.process_custom_days_min_input)(message, SimpleNamespace(language='ru'), MagicMock(), _state())

    update.assert_not_awaited()
    assert 'больше текущего максимума' in message.answer.await_args.args[0]


async def test_max_below_min_is_rejected(monkeypatch) -> None:
    tariff = _tariff(min_days=5, max_days=30)
    update = AsyncMock()
    message = _message('4')
    monkeypatch.setattr(mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(mod, 'update_tariff', update)

    await _unwrap(mod.process_custom_days_max_input)(message, SimpleNamespace(language='ru'), MagicMock(), _state())

    update.assert_not_awaited()
    assert 'меньше текущего минимума' in message.answer.await_args.args[0]
