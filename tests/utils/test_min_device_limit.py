"""Нижняя граница уменьшения количества устройств.

По умолчанию опустить лимит ниже включённого в тариф нельзя: уменьшают почти
всегда не ради самоограничения, а чтобы платить меньше — либо промахиваются и
потом спрашивают в поддержке, почему устройств меньше, чем положено по тарифу.
``ALLOW_DEVICES_BELOW_TARIFF_LIMIT=True`` возвращает прежнее поведение.
"""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.utils.subscription_utils import resolve_min_device_limit


def _tariff(device_limit):
    return SimpleNamespace(device_limit=device_limit)


def test_default_floor_is_tariff_device_limit(monkeypatch):
    monkeypatch.setattr(settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', False)
    assert resolve_min_device_limit(_tariff(3)) == 3
    assert resolve_min_device_limit(_tariff(1)) == 1


def test_opt_in_restores_previous_behaviour(monkeypatch):
    """Флаг возвращает прежний минимум 1 — для тех, кому нужно самоограничение."""
    monkeypatch.setattr(settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', True)
    assert resolve_min_device_limit(_tariff(5)) == 1


@pytest.mark.parametrize('value', [None, 0, '', 'мусор'])
def test_without_usable_tariff_limit_floor_is_one(monkeypatch, value):
    """Классический режим и битые значения не должны блокировать уменьшение."""
    monkeypatch.setattr(settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', False)
    assert resolve_min_device_limit(_tariff(value)) == 1


def test_no_tariff_at_all_floor_is_one(monkeypatch):
    monkeypatch.setattr(settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', False)
    assert resolve_min_device_limit(None) == 1
    assert resolve_min_device_limit() == 1


def _offered_counts(keyboard) -> list[int]:
    offered = []
    for row in keyboard.inline_keyboard:
        for button in row:
            data = button.callback_data or ''
            if data.startswith('change_devices_'):
                offered.append(int(data.rsplit('_', 1)[1]))
    return offered


def test_keyboard_hides_values_below_tariff_limit(monkeypatch):
    """Кнопки с запрещёнными значениями не должны показываться вовсе."""
    from app.keyboards.inline import get_change_devices_keyboard

    monkeypatch.setattr(settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', False)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 5000)

    tariff = SimpleNamespace(device_limit=3, max_device_limit=10, device_price_kopeks=5000)
    keyboard = get_change_devices_keyboard(current_devices=5, language='ru', tariff=tariff)

    offered = _offered_counts(keyboard)
    assert offered, 'клавиатура должна предлагать варианты'
    assert min(offered) >= 3


def test_keyboard_offers_lower_values_when_opted_in(monkeypatch):
    from app.keyboards.inline import get_change_devices_keyboard

    monkeypatch.setattr(settings, 'ALLOW_DEVICES_BELOW_TARIFF_LIMIT', True)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 10)
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 5000)

    tariff = SimpleNamespace(device_limit=3, max_device_limit=10, device_price_kopeks=5000)
    keyboard = get_change_devices_keyboard(current_devices=5, language='ru', tariff=tariff)

    assert min(_offered_counts(keyboard)) < 3
