"""Regression tests for the autopay-period feature shipped in 4c8705d1.

Covers the three highest-risk surfaces of that commit, which originally
landed with zero tests:

1. ``resolve_autopay_period_candidate`` — the period-validation helper that
   gates the three-tier resolution in ``_process_autopayments``. Originally
   defined as an inline closure that fail-OPEN'd when ``available_periods``
   was empty (tariff-less / classic-mode subscriptions); the fix extracts
   it to module-level and makes it fail-CLOSED by also consulting
   ``settings.get_available_renewal_periods()`` as a backup allowlist.

2. ``update_subscription_autopay`` sentinel semantics — the
   ``_AUTOPAY_PERIOD_UNSET`` object distinguishes "don't touch the column"
   from the explicit ``None`` ("clear to default"). A refactor that
   collapses the sentinel into a plain ``None`` default would silently
   wipe every user's autopay-period override on the next legacy-caller
   invocation.

3. User-facing ``set_autopay_period`` callback handler — the three suffix
   branches (``default`` / valid int / invalid int) decide whether to
   write, what to write, and whether to alert the user.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud import subscription as subscription_crud
from app.database.crud.subscription import _AUTOPAY_PERIOD_UNSET, update_subscription_autopay
from app.handlers.subscription import autopay as autopay_handler
from app.services import monitoring_service
from app.services.monitoring_service import resolve_autopay_period_candidate


def _make_tariff(available: list[int] | None) -> SimpleNamespace:
    return SimpleNamespace(get_available_periods=lambda: available)


class _StubSettings:
    """Plain object replacement for ``monitoring_service.settings`` in tests.

    pydantic-settings forbids ``setattr`` of non-field attributes, so we can't
    monkeypatch methods on the real instance — substitute the whole binding."""

    def __init__(self, renewal_periods: list[int]):
        self._renewal_periods = renewal_periods

    def get_available_renewal_periods(self) -> list[int]:
        return list(self._renewal_periods)


@pytest.mark.parametrize(
    'candidate, available, expected',
    [
        # Falsy / non-positive inputs reject immediately.
        (None, [30, 90], None),
        (0, [30, 90], None),
        (-7, [30, 90], None),
        # Valid candidate from the tariff allowlist.
        (30, [30, 90, 180], 30),
        (180, [30, 90, 180], 180),
        # Candidate NOT in the tariff allowlist — reject.
        (45, [30, 90, 180], None),
        # Edge: candidate exactly at boundary value.
        (90, [30, 90, 180], 90),
    ],
)
def test_resolve_autopay_period_candidate_with_tariff(candidate, available, expected):
    tariff = _make_tariff(available)
    assert resolve_autopay_period_candidate(candidate, tariff) == expected


def test_resolve_autopay_period_candidate_falls_back_to_global_when_tariff_has_no_periods(monkeypatch):
    """When the tariff has no priced periods, validation falls back to the global allowlist
    rather than fail-open. Closes the gap where an env-default value drifted past validation
    just because the tariff was misconfigured."""
    tariff = _make_tariff([])
    monkeypatch.setattr(monitoring_service, 'settings', _StubSettings([30, 60, 90]))

    assert resolve_autopay_period_candidate(30, tariff) == 30
    # 45 is in neither tariff (empty) nor global allowlist → reject.
    assert resolve_autopay_period_candidate(45, tariff) is None


def test_resolve_autopay_period_candidate_falls_back_to_global_when_no_tariff(monkeypatch):
    """Classic-mode (no tariff) subscriptions still need bounded periods — the global
    renewal-periods allowlist gates them. Without this guard a malicious DB write or env
    typo could ship 999-day extensions."""
    monkeypatch.setattr(monitoring_service, 'settings', _StubSettings([30, 60, 90]))

    assert resolve_autopay_period_candidate(30, None) == 30
    assert resolve_autopay_period_candidate(999, None) is None


def test_resolve_autopay_period_candidate_rejects_when_both_allowlists_empty(monkeypatch):
    """Fail-closed: with no allowlist available anywhere, ANY candidate is rejected and the
    caller falls through to the next tier (tariff.get_shortest_period() / 30-day floor)."""
    monkeypatch.setattr(monitoring_service, 'settings', _StubSettings([]))

    assert resolve_autopay_period_candidate(30, None) is None
    assert resolve_autopay_period_candidate(30, _make_tariff([])) is None


def test_resolve_autopay_period_candidate_swallows_broken_tariff(monkeypatch):
    """A tariff whose ``get_available_periods`` raises (corrupted period_prices, ORM lazy-load
    failure on detached session) must NOT crash autopay — fall through to global allowlist."""

    class BrokenTariff:
        def get_available_periods(self):
            raise RuntimeError('boom')

    monkeypatch.setattr(monitoring_service, 'settings', _StubSettings([30]))

    assert resolve_autopay_period_candidate(30, BrokenTariff()) == 30
    assert resolve_autopay_period_candidate(999, BrokenTariff()) is None


async def test_update_subscription_autopay_sentinel_does_not_touch_period_when_omitted():
    """Legacy callers (autopay.py:154, autopay.py:188, miniapp.py:3733) invoke with positional
    args only. They MUST not touch autopay_period_days — the sentinel default protects them."""
    subscription = SimpleNamespace(
        user_id=42,
        autopay_enabled=False,
        autopay_days_before=3,
        autopay_period_days=90,  # pre-existing user override
        updated_at=None,
    )
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    await update_subscription_autopay(db, subscription, enabled=True, days_before=5)

    assert subscription.autopay_enabled is True
    assert subscription.autopay_days_before == 5
    # CRITICAL: legacy caller must not wipe the user's chosen period.
    assert subscription.autopay_period_days == 90


async def test_update_subscription_autopay_explicit_none_clears_period():
    """When the user clicks "По умолчанию" in the period picker, the handler passes
    period_days=None — explicit clear, distinct from the sentinel default."""
    subscription = SimpleNamespace(
        user_id=42,
        autopay_enabled=True,
        autopay_days_before=3,
        autopay_period_days=90,
        updated_at=None,
    )
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    await update_subscription_autopay(db, subscription, enabled=True, period_days=None)

    assert subscription.autopay_period_days is None


async def test_update_subscription_autopay_explicit_int_sets_period():
    subscription = SimpleNamespace(
        user_id=42,
        autopay_enabled=True,
        autopay_days_before=3,
        autopay_period_days=None,
        updated_at=None,
    )
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    await update_subscription_autopay(db, subscription, enabled=True, period_days=180)

    assert subscription.autopay_period_days == 180


def test_autopay_period_unset_sentinel_is_module_private():
    """The sentinel must stay a private object — exporting it would tempt callers to
    pass it explicitly, defeating the sentinel pattern. Pin module-private status."""
    assert _AUTOPAY_PERIOD_UNSET is not None
    assert _AUTOPAY_PERIOD_UNSET is not False
    assert _AUTOPAY_PERIOD_UNSET not in {None, 0, ''}


async def test_set_autopay_period_default_suffix_clears_override(monkeypatch):
    """Suffix `default` → clear the per-subscription override (passes period_days=None).
    Also pins that `state` is forwarded to the menu redraw — without it, the post-action
    redraw loses FSM `active_subscription_id` and multi-tariff users land on
    'Выберите подписку'."""
    subscription = SimpleNamespace(id=1, autopay_enabled=True, tariff=_make_tariff([30, 90]))
    db = MagicMock()
    db.refresh = AsyncMock()
    state = SimpleNamespace()  # sentinel state object — we only verify it propagates

    update_mock = AsyncMock()
    menu_mock = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'update_subscription_autopay', update_mock)
    monkeypatch.setattr(
        autopay_handler,
        '_resolve_subscription',
        AsyncMock(return_value=(subscription, subscription.id)),
    )
    monkeypatch.setattr(autopay_handler, 'handle_autopay_menu', menu_mock)

    callback = SimpleNamespace(
        data='autopay_period_default',
        answer=AsyncMock(),
    )
    db_user = SimpleNamespace(language='ru')

    await autopay_handler.set_autopay_period(callback, db_user, db, state)

    update_mock.assert_awaited_once()
    call_kwargs = update_mock.await_args.kwargs
    assert call_kwargs.get('period_days') is None

    # Pin the state-forwarding contract — `handle_autopay_menu(callback, db_user, db, state)`.
    menu_mock.assert_awaited_once_with(callback, db_user, db, state)


async def test_set_autopay_period_valid_int_writes_period(monkeypatch):
    """Suffix matching a valid tariff period → write it to the subscription.
    Also pins state forwarding to the menu redraw (see default-suffix test docstring)."""
    subscription = SimpleNamespace(id=1, autopay_enabled=True, tariff=_make_tariff([30, 90, 180]))
    db = MagicMock()
    db.refresh = AsyncMock()
    state = SimpleNamespace()

    update_mock = AsyncMock()
    menu_mock = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'update_subscription_autopay', update_mock)
    monkeypatch.setattr(
        autopay_handler,
        '_resolve_subscription',
        AsyncMock(return_value=(subscription, subscription.id)),
    )
    monkeypatch.setattr(autopay_handler, 'handle_autopay_menu', menu_mock)

    callback = SimpleNamespace(
        data='autopay_period_90',
        answer=AsyncMock(),
    )
    db_user = SimpleNamespace(language='ru')

    await autopay_handler.set_autopay_period(callback, db_user, db, state)

    update_mock.assert_awaited_once()
    assert update_mock.await_args.kwargs.get('period_days') == 90
    menu_mock.assert_awaited_once_with(callback, db_user, db, state)


async def test_set_autopay_period_invalid_int_alerts_without_writing(monkeypatch):
    """Suffix matching an integer NOT in the tariff allowlist → alert the user and do NOT
    write. This is the safety net for tariff edits that removed a previously-valid period."""
    subscription = SimpleNamespace(id=1, autopay_enabled=True, tariff=_make_tariff([30, 90]))
    db = MagicMock()
    db.refresh = AsyncMock()

    update_mock = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'update_subscription_autopay', update_mock)
    monkeypatch.setattr(
        autopay_handler,
        '_resolve_subscription',
        AsyncMock(return_value=(subscription, subscription.id)),
    )
    monkeypatch.setattr(autopay_handler, 'handle_autopay_menu', AsyncMock())

    callback = SimpleNamespace(
        data='autopay_period_180',  # not in [30, 90]
        answer=AsyncMock(),
    )
    db_user = SimpleNamespace(language='ru')

    await autopay_handler.set_autopay_period(callback, db_user, db)

    update_mock.assert_not_awaited()
    callback.answer.assert_awaited_once()
    # Alert must be shown (show_alert=True) so user sees the rejection.
    assert callback.answer.await_args.kwargs.get('show_alert') is True
