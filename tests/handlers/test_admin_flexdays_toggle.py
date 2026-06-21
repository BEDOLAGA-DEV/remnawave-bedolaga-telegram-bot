"""Guard for the bot-admin flexible_days_enabled toggle.

A full behavioural test needs an aiogram dispatcher + DB; this is a light
source/import guard that the toggle handler exists, flips the flag, and is
wired to the admin_tariff_toggle_flexdays: callback.
"""

from __future__ import annotations

from pathlib import Path

import app.handlers.admin.tariffs as m


TARIFFS_PATH = Path(__file__).resolve().parents[2] / 'app' / 'handlers' / 'admin' / 'tariffs.py'


def test_toggle_handler_exists() -> None:
    assert hasattr(m, 'toggle_tariff_flexible_days')


def test_toggle_wired_and_flips_flag() -> None:
    src = TARIFFS_PATH.read_text(encoding='utf-8')
    # Button + handler registration both use the dedicated callback prefix.
    assert 'admin_tariff_toggle_flexdays:' in src
    # Handler is registered.
    assert "toggle_tariff_flexible_days, F.data.startswith('admin_tariff_toggle_flexdays:')" in src
    # Handler actually flips the flag via update_tariff.
    assert 'flexible_days_enabled=new_value' in src
