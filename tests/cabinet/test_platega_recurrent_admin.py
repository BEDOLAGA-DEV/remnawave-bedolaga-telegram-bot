"""Cabinet ADMIN view of Platega SBP auto-renewal status + admin cancel (Task 14).

Covers two things in ``app/cabinet/routes/admin_users.py``:

* ``_build_subscription_info_async`` gains ``sbp_recurring_status`` /
  ``sbp_recurring_id`` on the returned ``UserSubscriptionInfo`` — populated
  from ``get_active_platega_subscription_by_subscription`` when
  ``settings.is_platega_recurrent_enabled()`` is on, and left at their
  ``None`` defaults (no query at all) when the gate is off. The *sync*
  builder ``_build_subscription_info`` never touches these fields, gate or
  no gate.
* ``POST /{user_id}/subscriptions/{sub_id}/cancel-sbp-recurring`` — verifies
  the subscription belongs to ``user_id`` (IDOR guard via
  ``get_subscription_by_id_for_user``) before delegating to the same
  best-effort helper the reset/delete subscription flows already use
  (``cancel_platega_recurring_for_subscription_safe``, Task 11).

Route/builder functions are called directly (no HTTP client), mirroring
``tests/cabinet/test_admin_user_activity.py`` (the house pattern for
``admin_users.py``: ``require_permission``/``get_cabinet_db`` are bypassed by
passing already-resolved ``admin``/``db`` arguments straight to the
handler) and ``tests/cabinet/test_platega_recurrent_routes.py`` (the
Platega-specific gate/monkeypatch conventions).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_users
from app.config import settings


def _configure_gate(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Drive ``settings.is_platega_recurrent_enabled()`` to ``enabled``.

    Mirrors ``tests/cabinet/test_platega_recurrent_routes.py``'s
    ``_configure_gate``: PLATEGA_ENABLED + merchant/secret + the recurrent
    flag all need to be true for the gate to open.
    """
    values = {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'm',
        'PLATEGA_SECRET': 's',
        'PLATEGA_RECURRENT_ENABLED': enabled,
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)


def _subscription(**overrides) -> SimpleNamespace:
    base = dict(
        id=42,
        status='active',
        is_trial=False,
        start_date=None,
        end_date=None,
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=1,
        tariff_id=None,
        autopay_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _db_with_empty_traffic_purchases() -> AsyncMock:
    """DB stand-in for the unconditional TrafficPurchase query inside
    ``_build_subscription_info_async``."""
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    return db


# --- _build_subscription_info_async: sbp_recurring_* population -------------


async def test_async_builder_populates_sbp_status_when_gate_on(monkeypatch):
    _configure_gate(monkeypatch, enabled=True)
    subscription = _subscription()
    db = _db_with_empty_traffic_purchases()
    record = SimpleNamespace(status='ACTIVE', id=7)

    mock_get_active = AsyncMock(return_value=record)
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        mock_get_active,
    )

    info = await admin_users._build_subscription_info_async(db, subscription)

    assert info.sbp_recurring_status == 'ACTIVE'
    assert info.sbp_recurring_id == 7
    mock_get_active.assert_awaited_once_with(db, subscription.id)


async def test_async_builder_leaves_sbp_status_none_without_active_record(monkeypatch):
    """Gate on, but no active Platega subscription for this subscription_id."""
    _configure_gate(monkeypatch, enabled=True)
    subscription = _subscription()
    db = _db_with_empty_traffic_purchases()

    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=None),
    )

    info = await admin_users._build_subscription_info_async(db, subscription)

    assert info.sbp_recurring_status is None
    assert info.sbp_recurring_id is None


async def test_async_builder_skips_query_when_gate_off(monkeypatch):
    _configure_gate(monkeypatch, enabled=False)
    subscription = _subscription()
    db = _db_with_empty_traffic_purchases()

    async def _boom(*args, **kwargs):
        raise AssertionError('get_active_platega_subscription_by_subscription must not run when the gate is off')

    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        _boom,
    )

    info = await admin_users._build_subscription_info_async(db, subscription)

    assert info.sbp_recurring_status is None
    assert info.sbp_recurring_id is None


def test_sync_builder_never_sets_sbp_fields():
    """The sync builder has no DB access and must leave both fields at their
    ``None`` default regardless of the gate."""
    subscription = _subscription()

    info = admin_users._build_subscription_info(subscription)

    assert info.sbp_recurring_status is None
    assert info.sbp_recurring_id is None


# --- admin cancel endpoint ----------------------------------------------


async def test_route_registered(registered_paths):
    assert 'POST' in registered_paths.get(
        '/cabinet/admin/users/{user_id}/subscriptions/{sub_id}/cancel-sbp-recurring', set()
    )


async def test_cancel_sbp_recurring_owned_subscription_cancels_and_awaits_helper(monkeypatch):
    user_id = 1
    sub_id = 42
    subscription = _subscription(id=sub_id)
    db = AsyncMock()
    admin = SimpleNamespace(id=99)

    mock_get_owned = AsyncMock(return_value=subscription)
    mock_cancel = AsyncMock(return_value=None)
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_id_for_user', mock_get_owned)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)

    result = await admin_users.cancel_user_sbp_recurring(user_id=user_id, sub_id=sub_id, admin=admin, db=db)

    assert result == {'status': 'cancelled'}
    mock_get_owned.assert_awaited_once_with(db, sub_id, user_id)
    mock_cancel.assert_awaited_once_with(db, sub_id)


async def test_cancel_sbp_recurring_wrong_owner_404_and_helper_not_called(monkeypatch):
    db = AsyncMock()
    admin = SimpleNamespace(id=99)

    mock_get_owned = AsyncMock(return_value=None)
    mock_cancel = AsyncMock(return_value=None)
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_id_for_user', mock_get_owned)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.cancel_user_sbp_recurring(user_id=1, sub_id=42, admin=admin, db=db)

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    mock_cancel.assert_not_awaited()


async def test_cancel_sbp_recurring_missing_subscription_404(monkeypatch):
    """Same 404 path for a subscription_id that doesn't exist at all."""
    db = AsyncMock()
    admin = SimpleNamespace(id=99)

    mock_get_owned = AsyncMock(return_value=None)
    mock_cancel = AsyncMock(return_value=None)
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_id_for_user', mock_get_owned)
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', mock_cancel)

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.cancel_user_sbp_recurring(user_id=1, sub_id=999999, admin=admin, db=db)

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    assert exc_info.value.detail == 'Subscription not found'
    mock_cancel.assert_not_awaited()
