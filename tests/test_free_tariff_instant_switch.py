"""Regression: «Мгновенная смена тарифа» с бесплатного (0₽) тарифа.

Prorated instant-switch считает доплату как (ставка нового тарифа − ставка
текущего) × остаток дней. Для бесплатного тарифа, где у пользователя могут
копиться сотни дней (наспамленных или подаренных), это даёт абсурдные суммы
(полная цена нового тарифа за весь остаток, напр. +2580₽ при цене 200₽/мес)
и переносит бесплатные дни на платный тариф вопреки
TARIFF_SWITCH_RESET_FREE_DAYS (см. extend_subscription / test_free_tariff_day_reset).

Фикс: при TARIFF_SWITCH_RESET_FREE_DAYS=true бесплатный источник направляется
в флоу смены с выбором периода (tariff_switch) — оплачивается полная стоимость
выбранного периода, extend_subscription сбрасывает бесплатный остаток:
- кнопка «📦 Тариф» ведёт на tariff_switch, а не instant_switch;
- show/preview/confirm instant-switch редиректят в show_tariff_switch_list
  (защита от устаревших кнопок), списание по prorated-цене невозможно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.subscription.tariff_purchase as tp
import app.keyboards.inline as kb
from app.database.models import Tariff


FREE_TARIFF = Tariff(
    id=10,
    name='Бесплатно',
    is_active=True,
    is_daily=False,
    period_prices={'30': 0},
    daily_price_kopeks=0,
    traffic_limit_gb=0,
    device_limit=1,
)

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


# ── Клавиатура: кнопка «📦 Тариф» ──


def _callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


def _fake_sub(tariff) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        actual_status='active',
        status='active',
        tariff=tariff,
        traffic_limit_gb=0,
        end_date=None,
        is_daily_paused=False,
    )


def _patch_tariffs_mode(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_tariffs_mode', lambda self: True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(kb, 'get_display_subscription_link', lambda sub: None)


def test_keyboard_free_tariff_routes_to_period_switch(monkeypatch):
    _patch_tariffs_mode(monkeypatch)
    monkeypatch.setattr(kb.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)
    markup = kb.get_subscription_keyboard(
        'ru', has_subscription=True, is_trial=False, subscription=_fake_sub(FREE_TARIFF)
    )
    cbs = _callbacks(markup)
    assert 'tariff_switch' in cbs  # флоу с выбором периода (полная цена, сброс дней)
    assert 'instant_switch' not in cbs


def test_keyboard_free_tariff_keeps_instant_when_reset_disabled(monkeypatch):
    """TARIFF_SWITCH_RESET_FREE_DAYS=false — админ явно разрешил перенос бесплатных
    дней, prorated instant-switch остаётся доступен (старое поведение)."""
    _patch_tariffs_mode(monkeypatch)
    monkeypatch.setattr(kb.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)
    markup = kb.get_subscription_keyboard(
        'ru', has_subscription=True, is_trial=False, subscription=_fake_sub(FREE_TARIFF)
    )
    assert 'instant_switch' in _callbacks(markup)


def test_keyboard_paid_tariff_keeps_instant_switch(monkeypatch):
    _patch_tariffs_mode(monkeypatch)
    monkeypatch.setattr(kb.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)
    markup = kb.get_subscription_keyboard(
        'ru', has_subscription=True, is_trial=False, subscription=_fake_sub(PAID_TARIFF)
    )
    cbs = _callbacks(markup)
    assert 'instant_switch' in cbs  # платный источник — prorated-флоу не тронут
    assert 'tariff_switch' not in cbs


# ── Хендлеры instant-switch: редирект бесплатного источника ──


def _mk_callback(data: str = 'instant_switch') -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _mk_user() -> MagicMock:
    db_user = MagicMock()
    db_user.id = 1
    db_user.language = 'ru'
    db_user.balance_kopeks = 1_000_000
    return db_user


def _mk_state(data: dict | None = None) -> AsyncMock:
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=data or {})
    return state


def _free_sub() -> MagicMock:
    sub = MagicMock()
    sub.id = 1
    sub.tariff_id = FREE_TARIFF.id
    sub.end_date = datetime.now(UTC) + timedelta(days=387)
    return sub


def _patch_get_tariff(monkeypatch):
    async def fake_get(db, tariff_id):
        return {FREE_TARIFF.id: FREE_TARIFF, PAID_TARIFF.id: PAID_TARIFF}.get(tariff_id)

    monkeypatch.setattr(tp, 'get_tariff_by_id', fake_get)


def _patch_common(monkeypatch) -> AsyncMock:
    """Общие моки: резолвер подписки, тарифы, флаг сброса. Возвращает мок
    show_tariff_switch_list для проверки редиректа."""
    monkeypatch.setattr(tp.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)
    _patch_get_tariff(monkeypatch)
    sub = _free_sub()
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(sub, sub.id)))
    switch_list = AsyncMock()
    monkeypatch.setattr(tp, 'show_tariff_switch_list', switch_list)
    return switch_list


async def test_instant_list_redirects_free_source(monkeypatch):
    switch_list = _patch_common(monkeypatch)

    callback = _mk_callback('instant_switch')
    await tp.show_instant_switch_list(callback, _mk_user(), AsyncMock(), _mk_state())

    switch_list.assert_awaited_once()
    callback.message.edit_text.assert_not_called()  # prorated-список не показан


async def test_instant_preview_redirects_free_source(monkeypatch):
    """Устаревшая кнопка instant_sw_preview в старом сообщении не должна
    показывать prorated-доплату за весь бесплатный остаток."""
    switch_list = _patch_common(monkeypatch)

    callback = _mk_callback(f'instant_sw_preview:{PAID_TARIFF.id}')
    state = _mk_state({'current_tariff_id': FREE_TARIFF.id, 'remaining_days': 387})
    await tp.preview_instant_switch(callback, _mk_user(), AsyncMock(), state)

    switch_list.assert_awaited_once()
    callback.message.edit_text.assert_not_called()


async def test_instant_confirm_refuses_free_source_and_never_charges(monkeypatch):
    """Ядро фикса: подтверждение instant-switch с бесплатного тарифа не должно
    списывать prorated-доплату (2580₽ за 387 дн. в баге со скриншота)."""
    import app.database.crud.user as user_crud

    switch_list = _patch_common(monkeypatch)
    db_user = _mk_user()
    monkeypatch.setattr(user_crud, 'lock_user_for_pricing', AsyncMock(return_value=db_user))
    charge = AsyncMock()
    monkeypatch.setattr(tp, 'subtract_user_balance', charge)

    callback = _mk_callback(f'instant_sw_confirm:{PAID_TARIFF.id}')
    await tp.confirm_instant_switch(callback, db_user, AsyncMock(), _mk_state())

    switch_list.assert_awaited_once()
    charge.assert_not_called()


async def test_instant_list_keeps_prorated_flow_for_paid_source(monkeypatch):
    """Платный источник: prorated instant-switch работает как раньше."""
    from app.config import Settings

    monkeypatch.setattr(tp.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    _patch_get_tariff(monkeypatch)

    sub = _free_sub()
    sub.tariff_id = PAID_TARIFF.id
    monkeypatch.setattr(tp, '_resolve_switch_subscription', AsyncMock(return_value=(sub, sub.id)))
    switch_list = AsyncMock()
    monkeypatch.setattr(tp, 'show_tariff_switch_list', switch_list)
    monkeypatch.setattr(tp, 'get_tariffs_for_user', AsyncMock(return_value=[]))

    callback = _mk_callback('instant_switch')
    await tp.show_instant_switch_list(callback, _mk_user(), AsyncMock(), _mk_state())

    switch_list.assert_not_awaited()  # редиректа нет
    callback.message.edit_text.assert_called_once()  # instant-флоу отработал сам
