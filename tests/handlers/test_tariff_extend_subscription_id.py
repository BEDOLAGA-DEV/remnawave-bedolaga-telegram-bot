"""Regression for issue #3012: multi-tariff renewal must carry the chosen
subscription_id explicitly in the callback, NOT rely on the generic resolver that
reads the trailing callback segment as a subscription_id. The trailing segment of
tariff_extend / tariff_ext_confirm is the PERIOD (e.g. 30 days) — when it collided
with another subscription's id, the wrong subscription was renewed and charged.

These tests pin the callback format: subscription_id is the FIRST data segment,
the period stays last.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.handlers.subscription.tariff_purchase import (
    get_tariff_extend_confirm_keyboard,
    get_tariff_extend_keyboard,
)


def _callbacks(keyboard) -> list[str]:
    return [btn.callback_data for row in keyboard.inline_keyboard for btn in row]


def test_confirm_keyboard_puts_subscription_id_first_period_last():
    kb = get_tariff_extend_confirm_keyboard(subscription_id=22, tariff_id=2, period=30, language='ru')
    cbs = _callbacks(kb)
    assert 'tariff_ext_confirm:22:2:30' in cbs

    # The renewal callback must parse to (sub_id=22, tariff_id=2, period=30) —
    # the OLD format 'tariff_ext_confirm:2:30' made parts[-1]=30 look like a sub_id.
    confirm = next(c for c in cbs if c.startswith('tariff_ext_confirm:'))
    parts = confirm.split(':')
    assert parts[1] == '22'  # subscription_id (explicit)
    assert parts[2] == '2'  # tariff_id
    assert parts[3] == '30'  # period (last — no longer mistaken for a sub_id)


def test_extend_keyboard_embeds_subscription_id_before_tariff_and_period():
    tariff = MagicMock()
    tariff.id = 2
    tariff.period_prices = {'30': 10000, '90': 27000}

    kb = get_tariff_extend_keyboard(
        tariff,
        'ru',
        db_user=None,
        subscription_device_limit=None,
        subscription_id=22,
    )
    cbs = _callbacks(kb)

    extend_cbs = [c for c in cbs if c.startswith('tariff_extend:')]
    assert 'tariff_extend:22:2:30' in extend_cbs
    assert 'tariff_extend:22:2:90' in extend_cbs

    for c in extend_cbs:
        parts = c.split(':')
        assert parts[1] == '22'  # subscription_id first
        assert parts[2] == '2'  # tariff_id
        # period is the trailing segment — must NOT be in the sub_id position
        assert parts[3] in ('30', '90')
