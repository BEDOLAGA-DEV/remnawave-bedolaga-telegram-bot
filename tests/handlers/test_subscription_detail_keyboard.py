"""Pins the autopay button on the multi-tariff subscription detail card.

Regression: in multi-tariff mode (`MULTI_TARIFF_ENABLED=true`), the detail
keyboard for a single subscription used to show 6 buttons (link / extend /
traffic / devices / reissue / back). The 💳 Автоплатеж button was only
present in the legacy single-subscription menu, so users in multi-tariff
mode had no way to reach the autopay menu from a specific subscription.

This test file pins:
  1. The autopay button is present on active subscriptions
  2. The autopay button is NOT shown on expired/disabled subscriptions
     (no point auto-renewing what's already inactive — and the rest of
     the action set is also stripped for those statuses)
  3. The autopay button uses the legacy callback `subscription_autopay`
     without sub_id — multi-tariff resolution flows through FSM's
     active_subscription_id which `show_subscription_detail` must set.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.handlers.subscription import my_subscriptions
from app.handlers.subscription.my_subscriptions import (
    _build_subscription_detail_keyboard,
    show_subscription_detail,
)


def _callbacks(keyboard) -> list[str]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_autopay_button_present_for_active_subscription() -> None:
    sub = SimpleNamespace(actual_status='active')

    keyboard = _build_subscription_detail_keyboard(sub_id=42, sub=sub)

    callbacks = _callbacks(keyboard)
    assert 'subscription_autopay' in callbacks, (
        'Multi-tariff detail card must expose 💳 Автоплатеж; without this button '
        'users with multiple subscriptions have no entry point to the autopay menu.'
    )


def test_autopay_button_uses_legacy_callback_without_sub_id() -> None:
    """The button intentionally uses the existing `subscription_autopay` exact-match
    callback rather than a sub_id-encoded variant. Sub_id resolution flows through
    FSM `active_subscription_id`, set by show_subscription_detail. Changing this
    callback to e.g. `apm:{sub_id}` would require rewiring the entire autopay
    flow (toggle/days/period handlers + back buttons) — keep it as-is.

    Uses an unlikely sub_id (99999937) so a regression to substring-bake-in
    (`subscription_autopay_99999937`) cannot accidentally pass an exact-match
    check the way `'42' not in callback` could when the literal `42` doesn't
    appear in `subscription_autopay`."""
    sub_id = 99999937
    sub = SimpleNamespace(actual_status='active')

    keyboard = _build_subscription_detail_keyboard(sub_id=sub_id, sub=sub)

    autopay_buttons = [
        button for row in keyboard.inline_keyboard for button in row if button.callback_data == 'subscription_autopay'
    ]
    assert len(autopay_buttons) == 1
    # Exact match — refactor to `subscription_autopay_{id}` / `apm:{id}` must fail here.
    assert autopay_buttons[0].callback_data == 'subscription_autopay'
    assert str(sub_id) not in autopay_buttons[0].callback_data


def test_autopay_button_hidden_on_expired_subscription() -> None:
    sub = SimpleNamespace(actual_status='expired')

    keyboard = _build_subscription_detail_keyboard(sub_id=42, sub=sub)

    assert 'subscription_autopay' not in _callbacks(keyboard)


def test_autopay_button_hidden_on_disabled_subscription() -> None:
    sub = SimpleNamespace(actual_status='disabled')

    keyboard = _build_subscription_detail_keyboard(sub_id=42, sub=sub)

    assert 'subscription_autopay' not in _callbacks(keyboard)


def test_autopay_button_present_when_status_unknown() -> None:
    """When sub=None, the keyboard treats the subscription as active (is_inactive=False).
    The autopay button must be there too — symmetry with traffic/devices buttons that
    appear under the same condition."""
    keyboard = _build_subscription_detail_keyboard(sub_id=42, sub=None)

    assert 'subscription_autopay' in _callbacks(keyboard)


def _tariffs_mode(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(type(my_subscriptions.settings), 'is_tariffs_mode', lambda self: enabled)
    monkeypatch.setattr(my_subscriptions.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)


def _paid_sub(**over) -> SimpleNamespace:
    fields = {
        'actual_status': 'active',
        'tariff_id': 1,
        'is_trial': False,
        'tariff': SimpleNamespace(is_daily=False, is_free=False),
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def _switch_callbacks(sub) -> set[str]:
    callbacks = _callbacks(_build_subscription_detail_keyboard(sub_id=42, sub=sub))
    return {c for c in callbacks if c in ('instant_switch', 'tariff_switch')}


def test_tariff_switch_button_on_paid_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    """Мультитариф: на живой платной подписке есть «Сменить тариф» с пересчётом.

    Без кнопки человек мог только купить ещё одну подписку на другой тариф или
    просить админа поменять тариф руками."""
    _tariffs_mode(monkeypatch)

    assert _switch_callbacks(_paid_sub()) == {'instant_switch'}


def test_tariff_switch_goes_through_periods_for_daily_and_free_tariffs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Суточный и бесплатный тарифы меняются через список с выбором периода, как в одиночном режиме."""
    _tariffs_mode(monkeypatch)

    daily = _paid_sub(tariff=SimpleNamespace(is_daily=True, is_free=False))
    free = _paid_sub(tariff=SimpleNamespace(is_daily=False, is_free=True))

    assert _switch_callbacks(daily) == {'tariff_switch'}
    assert _switch_callbacks(free) == {'tariff_switch'}


@pytest.mark.parametrize(
    'over',
    [
        {'is_trial': True},
        {'actual_status': 'expired'},
        {'actual_status': 'disabled'},
        {'tariff_id': None},
    ],
)
def test_tariff_switch_button_hidden_where_switch_does_not_apply(monkeypatch: pytest.MonkeyPatch, over) -> None:
    """Триал покупают, а не меняют; истёкшую и отключённую продлевают; без тарифа менять нечего."""
    _tariffs_mode(monkeypatch)

    assert _switch_callbacks(_paid_sub(**over)) == set()


def test_tariff_switch_button_hidden_outside_tariffs_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _tariffs_mode(monkeypatch, enabled=False)

    assert _switch_callbacks(_paid_sub()) == set()


@pytest.mark.anyio('asyncio')
async def test_show_subscription_detail_writes_active_subscription_id_to_fsm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole multi-tariff autopay fix hinges on this side effect: opening a
    subscription detail card writes its id to FSM so downstream handlers that fire
    sub_id-less callbacks (subscription_autopay, etc.) can resolve the right sub.

    Without a test pinning this write, a refactor that removes the
    state.update_data(active_subscription_id=sub_id) line silently re-introduces
    the multi-tariff bug while the keyboard-structure tests remain green."""
    sub_id = 77
    subscription = SimpleNamespace(
        id=sub_id,
        actual_status='active',
        tariff=SimpleNamespace(name='X'),
        traffic_limit_gb=10,
        traffic_used_gb=1.0,
        device_limit=1,
        end_date=None,
        autopay_enabled=False,
        autopay_days_before=3,
    )

    monkeypatch.setattr(my_subscriptions, 'get_subscription_by_id_for_user', AsyncMock(return_value=subscription))

    state = SimpleNamespace(update_data=AsyncMock())
    db_user = SimpleNamespace(id=1, language='ru')
    callback = SimpleNamespace(
        data=f'sm:{sub_id}',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
    )

    try:
        await show_subscription_detail(callback, db_user, SimpleNamespace(), state)
    except Exception:
        # The handler may try to render text/keyboard using attrs we haven't fully mocked.
        # That's fine — we only care that the FSM write happened BEFORE any rendering.
        pass

    # The contract: active_subscription_id MUST be written with the resolved sub_id.
    state.update_data.assert_any_call(active_subscription_id=sub_id)


@pytest.mark.anyio('asyncio')
async def test_show_subscription_detail_does_not_write_fsm_on_idor_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the subscription doesn't belong to the requesting user (IDOR check returns
    None), the handler must short-circuit BEFORE writing to FSM. Otherwise a malicious
    callback with a foreign sub_id would poison the user's FSM with someone else's id."""
    monkeypatch.setattr(my_subscriptions, 'get_subscription_by_id_for_user', AsyncMock(return_value=None))

    state = SimpleNamespace(update_data=AsyncMock())
    db_user = SimpleNamespace(id=1, language='ru')
    callback = SimpleNamespace(
        data='sm:999',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )

    await show_subscription_detail(callback, db_user, SimpleNamespace(), state)

    state.update_data.assert_not_called()
    callback.answer.assert_awaited_once()
