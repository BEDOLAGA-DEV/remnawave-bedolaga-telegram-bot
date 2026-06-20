"""Regression (#3): on a fully EXPIRED subscription the "📦 Тариф" (change-tariff)
button used to be shown in the subscription menu, but the handler blocked the action
("Переключение недоступно") — a dead button. Now expired/disabled subs show
"📦 Купить тариф" (menu_buy / fresh purchase) instead, and active subs keep the
normal change-tariff button.
"""

from __future__ import annotations

from types import SimpleNamespace

import app.keyboards.inline as kb


def _callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


def _fake_sub(actual_status: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        actual_status=actual_status,
        status=status,
        tariff=None,  # → non-daily path
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
    )


def _patch_tariffs_mode(monkeypatch):
    from app.config import Settings

    # is_tariffs_mode/is_multi_tariff_enabled are methods on the pydantic Settings
    # class — patch them on the class (the singleton instance can't take new attrs).
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(kb, 'get_display_subscription_link', lambda sub: None)


def test_expired_sub_offers_buy_not_switch(monkeypatch):
    _patch_tariffs_mode(monkeypatch)
    markup = kb.get_subscription_keyboard(
        'ru', has_subscription=True, is_trial=False, subscription=_fake_sub('expired', 'expired')
    )
    cbs = _callbacks(markup)
    assert 'menu_buy' in cbs  # fresh-purchase entry shown instead
    assert 'instant_switch' not in cbs
    assert 'tariff_switch' not in cbs


def test_disabled_sub_offers_buy_not_switch(monkeypatch):
    _patch_tariffs_mode(monkeypatch)
    markup = kb.get_subscription_keyboard(
        'ru', has_subscription=True, is_trial=False, subscription=_fake_sub('disabled', 'disabled')
    )
    cbs = _callbacks(markup)
    assert 'menu_buy' in cbs
    assert 'instant_switch' not in cbs


def test_active_sub_keeps_change_tariff(monkeypatch):
    _patch_tariffs_mode(monkeypatch)
    markup = kb.get_subscription_keyboard(
        'ru', has_subscription=True, is_trial=False, subscription=_fake_sub('active', 'active')
    )
    cbs = _callbacks(markup)
    assert 'instant_switch' in cbs  # normal switch flow untouched
    assert 'menu_buy' not in cbs


def test_limited_sub_keeps_change_tariff(monkeypatch):
    """'limited' = traffic exhausted but time remaining (end_date>now) — switch still valid."""
    _patch_tariffs_mode(monkeypatch)
    markup = kb.get_subscription_keyboard(
        'ru', has_subscription=True, is_trial=False, subscription=_fake_sub('limited', 'limited')
    )
    cbs = _callbacks(markup)
    assert 'instant_switch' in cbs
