"""Integration-style tests for the СБП-автопродление handlers
(``handle_sbp_recurring_menu`` / ``_enable`` / ``_cancel``, plus the daily-tariff
SBP-entry reachability branch inside ``handle_autopay_menu``,
``app/handlers/subscription/autopay.py``).

Drives the real handler functions with the heavy I/O mocked (DB session,
Platega network calls via the module-level helpers, subscription
resolution), following the same style as
``tests/handlers/test_device_rename_cancel.py``: real handler code path,
``MagicMock``/``AsyncMock`` stand-ins for ``callback``/``db_user``/``db``,
``monkeypatch`` on the exact seam each handler calls through.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.subscription.autopay as autopay_mod
from app.config import settings


def _configure_gate(monkeypatch, enabled: bool) -> None:
    """Same fields as test_platega_recurrent_cancel_hooks.py's _configure_gate_on,
    toggled both ways — is_platega_recurrent_enabled() reads all four."""
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', enabled, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MERCHANT_ID', ('m' if enabled else None), raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_SECRET', ('s' if enabled else None), raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_RECURRENT_ENABLED', enabled, raising=False)


def _make_callback():
    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _make_user():
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    return user


def _keyboard_callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


def _keyboard_urls(markup) -> list[str]:
    return [btn.url for row in markup.inline_keyboard for btn in row if btn.url]


# --- handle_autopay_menu (daily-tariff SBP-entry reachability) ---


async def test_autopay_menu_daily_tariff_still_shows_sbp_entry(monkeypatch):
    """Daily-tariff subscriptions can't use balance-autopay, but Platega's SBP
    auto-renewal supports the `day` interval on the backend — the daily-tariff
    early return in handle_autopay_menu must still surface a reachable
    '⚡ Автопродление через СБП' button instead of a dead-end alert.
    Fails before Fix 1 (daily branch only called callback.answer() and never
    touched the message/keyboard, so no button was ever reachable)."""
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(is_daily=True))

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))

    await autopay_mod.handle_autopay_menu(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    assert 'sbp_recurring_menu' in _keyboard_callbacks(kwargs['reply_markup'])


async def test_autopay_menu_daily_tariff_gate_off_hides_sbp_entry(monkeypatch):
    """Counterpart of the reachability test: with the Platega recurrent gate
    OFF, the daily-tariff screen must not offer a dead SBP button."""
    _configure_gate(monkeypatch, False)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(is_daily=True))

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))

    await autopay_mod.handle_autopay_menu(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    assert 'sbp_recurring_menu' not in _keyboard_callbacks(kwargs['reply_markup'])


# --- handle_sbp_recurring_menu ---


async def test_menu_gate_off_shows_alert_and_does_not_render(monkeypatch):
    _configure_gate(monkeypatch, False)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()

    await autopay_mod.handle_sbp_recurring_menu(cb, user, db)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get('show_alert') is True
    cb.message.edit_text.assert_not_awaited()


async def test_menu_no_active_record_shows_enable_button(monkeypatch):
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=None),
    )

    await autopay_mod.handle_sbp_recurring_menu(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    callbacks = _keyboard_callbacks(kwargs['reply_markup'])
    assert 'sbp_recurring_enable' in callbacks
    assert 'sbp_recurring_cancel' not in callbacks


async def test_menu_active_record_shows_cancel_button(monkeypatch):
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10)
    record = SimpleNamespace(status='ACTIVE', next_charge_at=None)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=record),
    )

    await autopay_mod.handle_sbp_recurring_menu(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    callbacks = _keyboard_callbacks(kwargs['reply_markup'])
    assert 'sbp_recurring_cancel' in callbacks
    assert 'sbp_recurring_enable' not in callbacks


async def test_menu_no_subscription_shows_alert(monkeypatch):
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(None, None)))

    await autopay_mod.handle_sbp_recurring_menu(cb, user, db)

    cb.message.edit_text.assert_not_awaited()


# --- handle_sbp_recurring_enable ---


async def test_enable_gate_off_shows_alert(monkeypatch):
    _configure_gate(monkeypatch, False)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()

    await autopay_mod.handle_sbp_recurring_enable(cb, user, db)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get('show_alert') is True
    cb.message.edit_text.assert_not_awaited()


async def test_enable_trial_subscription_blocked_before_helper(monkeypatch):
    """Trial subscriptions must not be able to authorize a real recurring bank
    payment via SBP — same guard as toggle_autopay's balance-autopay enable
    path (`if subscription.is_trial or subscription.is_trial is None`).
    Fails before Fix 2 (only `tariff is not None` was checked; a trial
    subscription with a tariff_id sailed straight through to the helper)."""
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(id=5), is_trial=True)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_enable = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.enable_platega_sbp_recurring', mock_enable)

    await autopay_mod.handle_sbp_recurring_enable(cb, user, db)

    mock_enable.assert_not_awaited()
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get('show_alert') is True
    cb.message.edit_text.assert_not_awaited()


async def test_enable_without_tariff_shows_alert_and_skips_helper(monkeypatch):
    """No tariff on the subscription -> must short-circuit BEFORE calling the
    enable helper: create_platega_sbp_subscription has no None-guard on
    tariff.get_purchasable_price_for_period() (contract from Task 5), so
    calling through would be an AttributeError, not a friendly message."""
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=None, is_trial=False)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_enable = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.enable_platega_sbp_recurring', mock_enable)

    await autopay_mod.handle_sbp_recurring_enable(cb, user, db)

    mock_enable.assert_not_awaited()
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get('show_alert') is True


async def test_enable_value_error_shows_friendly_alert(monkeypatch):
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(id=5), is_trial=False)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(
        'app.services.payment.platega.enable_platega_sbp_recurring',
        AsyncMock(side_effect=ValueError('no price for period')),
    )

    await autopay_mod.handle_sbp_recurring_enable(cb, user, db)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get('show_alert') is True
    cb.message.edit_text.assert_not_awaited()


async def test_enable_runtime_error_shows_friendly_alert(monkeypatch):
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(id=5), is_trial=False)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(
        'app.services.payment.platega.enable_platega_sbp_recurring',
        AsyncMock(side_effect=RuntimeError('platega down')),
    )

    await autopay_mod.handle_sbp_recurring_enable(cb, user, db)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get('show_alert') is True
    cb.message.edit_text.assert_not_awaited()


async def test_enable_success_shows_redirect_url_button(monkeypatch):
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(id=5), is_trial=False)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(
        'app.services.payment.platega.enable_platega_sbp_recurring',
        AsyncMock(
            return_value={
                'local_id': 1,
                'platega_subscription_id': 'tx-1',
                'redirect_url': 'https://pay.example/1',
                'status': 'PENDING',
            }
        ),
    )

    await autopay_mod.handle_sbp_recurring_enable(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    assert 'https://pay.example/1' in _keyboard_urls(kwargs['reply_markup'])


async def test_enable_idempotent_return_without_redirect_shows_status(monkeypatch):
    """Idempotent return (already-active record) may carry no redirect_url —
    must not build a dead url=None button (Telegram would reject it); falls
    back to re-rendering the status view instead."""
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10, tariff=SimpleNamespace(id=5), is_trial=False)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(
        'app.services.payment.platega.enable_platega_sbp_recurring',
        AsyncMock(
            return_value={
                'local_id': 1,
                'platega_subscription_id': 'tx-1',
                'redirect_url': None,
                'status': 'ACTIVE',
            }
        ),
    )
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=SimpleNamespace(status='ACTIVE', next_charge_at=None)),
    )

    await autopay_mod.handle_sbp_recurring_enable(cb, user, db)

    # Falls through to handle_sbp_recurring_menu, which edits with the status view.
    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    assert _keyboard_urls(kwargs['reply_markup']) == []
    assert 'sbp_recurring_cancel' in _keyboard_callbacks(kwargs['reply_markup'])


# --- handle_sbp_recurring_cancel ---


async def test_cancel_works_even_when_gate_off(monkeypatch):
    """Отмена НЕ гейтится (паритет с кабинетным cancel): выключение фичи при
    живых привязках не останавливает списания Platega — юзер с существующей
    кнопкой отмены обязан суметь отписаться. Меню при выключенном флаге не
    перерисовывается (оно гейтится) — второго алерта «недоступно» нет."""
    _configure_gate(monkeypatch, False)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_cancel = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)

    await autopay_mod.handle_sbp_recurring_cancel(cb, user, db)

    mock_cancel.assert_awaited_once_with(db, 10)
    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_not_awaited()


async def test_cancel_gate_on_calls_helper_and_refreshes_menu(monkeypatch):
    _configure_gate(monkeypatch, True)
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    subscription = SimpleNamespace(id=10)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_cancel = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=None),
    )

    await autopay_mod.handle_sbp_recurring_cancel(cb, user, db)

    mock_cancel.assert_awaited_once_with(db, 10)
    # handle_sbp_recurring_cancel re-invokes handle_sbp_recurring_menu, which
    # re-renders the (now inactive) status view.
    cb.message.edit_text.assert_awaited_once()
    _, kwargs = cb.message.edit_text.call_args
    assert 'sbp_recurring_enable' in _keyboard_callbacks(kwargs['reply_markup'])


# --- toggle_autopay (reverse mutual exclusion) -------------------------------
#
# Task 5's create_platega_sbp_subscription already implements the forward
# direction: enabling SBP disables balance-autopay. These tests cover the
# missing reverse hook — enabling balance-autopay must best-effort cancel any
# active Platega SBP recurring subscription on the same subscription, or both
# renewal engines would drive it in parallel and double-charge the user.


def _autopay_subscription(**overrides) -> SimpleNamespace:
    base = dict(id=10, tariff_id=5, tariff=None, is_trial=False, autopay_enabled=False)
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_toggle_autopay_enable_cancels_active_sbp_recurring(monkeypatch):
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    cb.data = 'autopay_enable'
    subscription = _autopay_subscription()

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(autopay_mod, 'update_subscription_autopay', AsyncMock(return_value=subscription))
    monkeypatch.setattr(autopay_mod, 'handle_autopay_menu', AsyncMock())
    mock_cancel = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)

    await autopay_mod.toggle_autopay(cb, user, db)

    mock_cancel.assert_awaited_once_with(db, 10)


async def test_toggle_autopay_disable_does_not_touch_sbp(monkeypatch):
    """Disabling balance-autopay must NOT cancel SBP — only the enable path
    triggers the reverse mutual-exclusion cancel; SBP has its own independent
    cancel flow (handle_sbp_recurring_cancel)."""
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    cb.data = 'autopay_disable'
    subscription = _autopay_subscription(autopay_enabled=True)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    monkeypatch.setattr(autopay_mod, 'update_subscription_autopay', AsyncMock(return_value=subscription))
    monkeypatch.setattr(autopay_mod, 'handle_autopay_menu', AsyncMock())
    mock_cancel = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)

    await autopay_mod.toggle_autopay(cb, user, db)

    mock_cancel.assert_not_awaited()


async def test_toggle_autopay_enable_blocked_before_cancel_for_trial(monkeypatch):
    """A trial subscription is rejected before update_subscription_autopay is
    even reached — the SBP-cancel hook must not fire on a rejected toggle."""
    cb, user, db = _make_callback(), _make_user(), AsyncMock()
    cb.data = 'autopay_enable'
    subscription = _autopay_subscription(is_trial=True)

    monkeypatch.setattr(autopay_mod, '_resolve_subscription', AsyncMock(return_value=(subscription, 10)))
    mock_update = AsyncMock(return_value=subscription)
    monkeypatch.setattr(autopay_mod, 'update_subscription_autopay', mock_update)
    mock_cancel = AsyncMock()
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)

    await autopay_mod.toggle_autopay(cb, user, db)

    mock_update.assert_not_awaited()
    mock_cancel.assert_not_awaited()


# --- СБП-кнопка на экране подтверждения покупки тарифа ---


def _gate(monkeypatch, enabled: bool) -> None:
    for key, value in {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'm',
        'PLATEGA_SECRET': 's',
        'PLATEGA_RECURRENT_ENABLED': enabled,
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)


def _keyboard_callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


def test_tariff_confirm_keyboard_shows_sbp_button_when_gate_on(monkeypatch):
    from app.handlers.subscription.tariff_purchase import get_tariff_confirm_keyboard

    _gate(monkeypatch, True)
    callbacks = _keyboard_callbacks(get_tariff_confirm_keyboard(5, 30, 'ru'))
    assert 'tariff_sbp:5' in callbacks
    assert 'tariff_confirm:5:30' in callbacks


def test_tariff_confirm_keyboard_hides_sbp_button_when_gate_off(monkeypatch):
    from app.handlers.subscription.tariff_purchase import get_tariff_confirm_keyboard

    _gate(monkeypatch, False)
    callbacks = _keyboard_callbacks(get_tariff_confirm_keyboard(5, 30, 'ru'))
    assert not any(cb.startswith('tariff_sbp:') for cb in callbacks)


def test_daily_tariff_confirm_keyboard_gates_sbp_button(monkeypatch):
    from app.handlers.subscription.tariff_purchase import get_daily_tariff_confirm_keyboard

    _gate(monkeypatch, True)
    assert 'tariff_sbp:7' in _keyboard_callbacks(get_daily_tariff_confirm_keyboard(7, 'ru'))
    _gate(monkeypatch, False)
    assert not any(
        cb.startswith('tariff_sbp:') for cb in _keyboard_callbacks(get_daily_tariff_confirm_keyboard(7, 'ru'))
    )
