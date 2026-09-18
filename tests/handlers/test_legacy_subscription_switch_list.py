"""Список тарифов для старой подписки в боте.

Старая подписка — платная, без тарифа, при включённом режиме тарифов. Кнопка
«Перейти на тариф» ведёт в список тарифов, и там для неё не действуют
ограничения смены тарифа: это не смена (тарифа нет), а первый выбор. Раньше
выключенные оператором «повышение/понижение» давали тупик «Смена тарифа
недоступна», а истёкшая старая подписка получала «Оформите новый тариф с
нуля» — то есть вторую подписку вместо перевода этой же.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.subscription.tariff_purchase as tp
from app.config import Settings
from app.database.models import Tariff, User


PAID_TARIFF = Tariff(
    id=5,
    name='Премиум',
    is_active=True,
    is_daily=False,
    period_prices={'30': 20000},
    daily_price_kopeks=0,
    traffic_limit_gb=0,
    device_limit=8,
)


def _callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


def _legacy_sub(end_date: datetime) -> MagicMock:
    sub = MagicMock()
    sub.id = 1
    sub.tariff_id = None
    sub.is_trial = False
    sub.end_date = end_date
    return sub


def _user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = 1
    user.language = 'ru'
    user.promo_group_id = None
    user.promo_group = None
    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_expires_at = None
    user.get_primary_promo_group = MagicMock(return_value=None)
    user.get_promo_discount = MagicMock(return_value=0)
    return user


def _callback() -> MagicMock:
    callback = MagicMock()
    callback.data = 'tariff_switch'
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _state() -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


def _patch(monkeypatch, sub, *, switch_enabled: bool) -> None:
    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(tp.settings, 'TARIFF_SWITCH_UPGRADE_ENABLED', switch_enabled)
    monkeypatch.setattr(tp.settings, 'TARIFF_SWITCH_DOWNGRADE_ENABLED', switch_enabled)
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(sub, sub.id)))
    monkeypatch.setattr(tp, 'get_tariffs_for_user', AsyncMock(return_value=[PAID_TARIFF]))
    monkeypatch.setattr(tp, 'get_active_subscriptions_by_user_id', AsyncMock(return_value=[sub]))

    async def fake_get_tariff(db, tariff_id):
        return {PAID_TARIFF.id: PAID_TARIFF}.get(tariff_id)

    monkeypatch.setattr(tp, 'get_tariff_by_id', fake_get_tariff)


async def _shown_markup_and_text(callback) -> tuple[list[str], str]:
    callback.message.edit_text.assert_awaited_once()
    args, kwargs = callback.message.edit_text.await_args
    text = args[0] if args else kwargs['text']
    return _callbacks(kwargs['reply_markup']), text


@pytest.mark.asyncio
async def test_legacy_subscription_sees_tariffs_when_switching_is_disabled(monkeypatch):
    sub = _legacy_sub(datetime.now(UTC) + timedelta(days=10))
    _patch(monkeypatch, sub, switch_enabled=False)
    callback = _callback()

    await tp.show_tariff_switch_list(callback, _user(), AsyncMock(), _state())

    cbs, text = await _shown_markup_and_text(callback)
    assert f'tariff_sw_select:{PAID_TARIFF.id}' in cbs, text


@pytest.mark.asyncio
async def test_expired_legacy_subscription_sees_tariffs(monkeypatch):
    """Истёкшая старая подписка переводится на тариф той же строкой, а не покупкой с нуля."""
    sub = _legacy_sub(datetime.now(UTC) - timedelta(days=1))
    _patch(monkeypatch, sub, switch_enabled=True)
    callback = _callback()

    await tp.show_tariff_switch_list(callback, _user(), AsyncMock(), _state())

    cbs, text = await _shown_markup_and_text(callback)
    assert f'tariff_sw_select:{PAID_TARIFF.id}' in cbs, text
    assert 'menu_buy' not in cbs


@pytest.mark.asyncio
async def test_legacy_subscription_list_does_not_say_unknown_tariff(monkeypatch):
    """Человеку не пишем «Текущий: Неизвестно» — у старой подписки тарифа просто нет."""
    sub = _legacy_sub(datetime.now(UTC) + timedelta(days=10))
    _patch(monkeypatch, sub, switch_enabled=True)
    callback = _callback()

    await tp.show_tariff_switch_list(callback, _user(), AsyncMock(), _state())

    _, text = await _shown_markup_and_text(callback)
    assert 'Неизвестно' not in text
    assert 'подписка без тарифа' in text, text


@pytest.mark.asyncio
async def test_legacy_subscription_period_confirmation_does_not_say_unknown_tariff(monkeypatch):
    """Экран подтверждения после выбора тарифа тоже без «Текущий тариф: Неизвестно»."""
    from types import SimpleNamespace

    from app.services import pricing_engine as pricing_module

    sub = _legacy_sub(datetime.now(UTC) + timedelta(days=10))
    _patch(monkeypatch, sub, switch_enabled=True)
    price = SimpleNamespace(final_total=20000, original_total=20000, promo_group_discount=0, promo_offer_discount=0)
    monkeypatch.setattr(pricing_module.pricing_engine, 'calculate_tariff_purchase_price', AsyncMock(return_value=price))
    user = _user()
    user.balance_kopeks = 100_000
    callback = _callback()
    callback.data = f'tariff_sw_period:{PAID_TARIFF.id}:30'
    state = _state()
    state.get_data = AsyncMock(return_value={'current_tariff_id': None, 'active_subscription_id': sub.id})

    await tp.select_tariff_switch_period(callback, user, AsyncMock(), state)

    _, text = await _shown_markup_and_text(callback)
    assert 'Неизвестно' not in text
    assert 'подписка без тарифа' in text, text
