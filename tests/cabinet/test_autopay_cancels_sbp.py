"""Reverse mutual-exclusion: enabling balance-autopay cancels an active
Platega SBP recurring subscription — cabinet endpoint
(``app/cabinet/routes/subscription_modules/autopay.py::update_autopay``).

Task 5's ``create_platega_sbp_subscription`` already implements the forward
direction: enabling SBP disables balance-autopay
(``test_create_sbp_subscription_persists_and_disables_autopay``,
``tests/services/test_platega_subscription_callbacks.py``). This covers the
missing reverse hook — without it, a user could enable SBP, then re-enable
balance-autopay from the cabinet, and both renewal engines would drive the
same subscription in parallel -> double charge.

Route function is called directly (no HTTP client), mirroring
``tests/cabinet/test_platega_recurrent_routes.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.cabinet.routes.subscription_modules import autopay as route
from app.cabinet.schemas.subscription import AutopayUpdateRequest
from app.database.models import User


def _user() -> User:
    return User(id=1, telegram_id=123456)


def _subscription(**overrides) -> SimpleNamespace:
    base = dict(
        id=42,
        tariff_id=5,
        tariff=None,
        is_trial=False,
        autopay_enabled=False,
        autopay_days_before=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_enable_autopay_cancels_active_sbp_recurring(monkeypatch):
    subscription = _subscription()
    db = AsyncMock()

    async def fake_resolve(resolve_db, u, subscription_id):
        assert resolve_db is db
        return subscription

    mock_cancel = AsyncMock()
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.helpers.resolve_subscription',
        fake_resolve,
    )
    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        mock_cancel,
    )

    result = await route.update_autopay(
        AutopayUpdateRequest(enabled=True),
        user=_user(),
        db=db,
        subscription_id=None,
    )

    assert result['autopay_enabled'] is True
    mock_cancel.assert_awaited_once_with(db, subscription.id)


async def test_disable_autopay_does_not_cancel_sbp(monkeypatch):
    """Disabling balance-autopay must NOT touch SBP — only the enable path
    triggers the reverse mutual-exclusion cancel."""
    subscription = _subscription(autopay_enabled=True)
    db = AsyncMock()

    async def fake_resolve(resolve_db, u, subscription_id):
        return subscription

    mock_cancel = AsyncMock()
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.helpers.resolve_subscription',
        fake_resolve,
    )
    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        mock_cancel,
    )

    result = await route.update_autopay(
        AutopayUpdateRequest(enabled=False),
        user=_user(),
        db=db,
        subscription_id=None,
    )

    assert result['autopay_enabled'] is False
    mock_cancel.assert_not_awaited()


async def test_enable_autopay_rejected_for_trial_does_not_cancel_sbp(monkeypatch):
    """A rejected enable (trial subscription -> 400) must not fire the
    SBP-cancel hook — the toggle never actually took effect."""
    from fastapi import HTTPException

    subscription = _subscription(is_trial=True)
    db = AsyncMock()

    async def fake_resolve(resolve_db, u, subscription_id):
        return subscription

    mock_cancel = AsyncMock()
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.helpers.resolve_subscription',
        fake_resolve,
    )
    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        mock_cancel,
    )

    try:
        await route.update_autopay(
            AutopayUpdateRequest(enabled=True),
            user=_user(),
            db=db,
            subscription_id=None,
        )
        raise AssertionError('expected HTTPException for trial subscription')
    except HTTPException as exc:
        assert exc.status_code == 400

    mock_cancel.assert_not_awaited()
    db.commit.assert_not_awaited()
