"""Cabinet user endpoints for Platega SBP auto-renewal (Task 13).

Covers the three subscription-scoped endpoints in
``app/cabinet/routes/subscription_modules/platega_recurrent.py``:

* ``POST /subscription/platega-recurrent/enable`` — creates the recurring
  SBP subscription for the resolved subscription's tariff (Task 12's
  ``enable_platega_sbp_recurring``).
* ``GET /subscription/platega-recurrent`` — current auto-renewal state.
* ``POST /subscription/platega-recurrent/cancel`` — best-effort cancel
  (Task 12's ``cancel_platega_recurring_for_subscription_safe``).

All three must gate on ``settings.is_platega_recurrent_enabled()`` *before*
touching ``resolve_subscription`` or the database. The enable endpoint must
load the tariff explicitly via ``get_tariff_by_id`` rather than an async
lazy-load off ``subscription.tariff`` — that pattern crashes with
MissingGreenlet outside a live request context (see memory entry
``daily_resume_missinggreenlet.md``), so the happy-path tests use a
subscription stand-in whose ``.tariff`` property raises if touched.

Route functions are called directly (no HTTP client), mirroring
``tests/cabinet/test_ticket_cabinet_guards.py`` and
``tests/services/test_platega_recurrent_cancel_hooks.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.subscription_modules import platega_recurrent as route
from app.config import settings
from app.database.models import User


@pytest.fixture
def user() -> User:
    return User(id=1, telegram_id=123456)


class _Subscription:
    """Minimal subscription stand-in.

    ``tariff`` is a property that raises if touched — the enable endpoint
    must fetch the tariff explicitly via ``get_tariff_by_id`` instead of an
    async lazy-load off ``subscription.tariff``.
    """

    def __init__(self, *, id: int = 10, tariff_id: int | None = 5) -> None:
        self.id = id
        self.tariff_id = tariff_id

    @property
    def tariff(self):
        raise AssertionError('subscription.tariff must not be lazily accessed — use get_tariff_by_id')


def _tariff() -> SimpleNamespace:
    return SimpleNamespace(id=5, name='Стандарт', is_daily=False)


def _configure_gate(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Drive ``settings.is_platega_recurrent_enabled()`` to ``enabled``.

    Mirrors ``tests/services/test_platega_recurrent_cancel_hooks.py``'s
    ``_configure_gate_on``: PLATEGA_ENABLED + merchant/secret + the recurrent
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


# --- Gate: 403 before touching resolve_subscription/db ----------------------


async def test_enable_403_when_gate_disabled(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=False)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await route.enable_platega_recurrent(user=user, db=db, subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    db.execute.assert_not_awaited()


async def test_get_403_when_gate_disabled(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=False)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await route.get_platega_recurrent(user=user, db=db, subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    db.execute.assert_not_awaited()


async def test_cancel_works_even_when_gate_disabled(monkeypatch, user):
    """Отмена НЕ гейтится: выключение фичи не должно бросать юзеров с живыми
    привязками, по которым Platega продолжает списывать без кнопки отписки."""
    _configure_gate(monkeypatch, enabled=False)
    subscription = _Subscription()
    db = AsyncMock()

    async def fake_resolve(resolve_db, u, subscription_id):
        return subscription

    mock_cancel = AsyncMock(return_value=None)
    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        mock_cancel,
    )

    result = await route.cancel_platega_recurrent(user=user, db=db, subscription_id=None)

    assert result == {'status': 'cancelled'}
    mock_cancel.assert_awaited_once_with(db, subscription.id)


# --- enable -------------------------------------------------------------


async def test_enable_404_when_no_subscription(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)

    async def fake_resolve(db, u, subscription_id):
        return None

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)

    with pytest.raises(HTTPException) as exc_info:
        await route.enable_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


async def test_enable_400_for_trial_subscription(monkeypatch, user):
    """Триал — пробник: подключать к нему рекуррентное списание нельзя
    (списывали бы полную цену тарифа за бесплатный период)."""
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    subscription.is_trial = True

    async def fake_resolve(db, u, subscription_id):
        return subscription

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)

    with pytest.raises(HTTPException) as exc_info:
        await route.enable_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert 'Trial' in exc_info.value.detail


async def test_enable_400_when_subscription_has_no_tariff(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription(tariff_id=None)

    async def fake_resolve(db, u, subscription_id):
        return subscription

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)

    with pytest.raises(HTTPException) as exc_info:
        await route.enable_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == 'Subscription has no tariff'


async def test_enable_400_when_tariff_not_found(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()

    async def fake_resolve(db, u, subscription_id):
        return subscription

    async def fake_get_tariff(db, tariff_id, **kwargs):
        assert tariff_id == subscription.tariff_id

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', fake_get_tariff)

    with pytest.raises(HTTPException) as exc_info:
        await route.enable_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == 'Tariff not found'


async def test_enable_happy_path_returns_status_and_redirect(monkeypatch, user):
    """Gate on, tariff loaded explicitly, helper succeeds -> {status, redirect_url}.

    Extra fields the Task 12 helper returns (``local_id``,
    ``platega_subscription_id``) must NOT leak into the response shape.
    """
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    tariff = _tariff()
    db = AsyncMock()

    async def fake_resolve(resolve_db, u, subscription_id):
        assert resolve_db is db
        assert u is user
        return subscription

    mock_get_tariff = AsyncMock(return_value=tariff)
    mock_enable = AsyncMock(
        return_value={
            'status': 'PENDING',
            'redirect_url': 'https://pay/x',
            'local_id': 1,
            'platega_subscription_id': 'ps',
        }
    )

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', mock_get_tariff)
    monkeypatch.setattr('app.services.payment.platega.enable_platega_sbp_recurring', mock_enable)

    result = await route.enable_platega_recurrent(user=user, db=db, subscription_id=None)

    assert result == {'status': 'PENDING', 'redirect_url': 'https://pay/x'}
    mock_get_tariff.assert_awaited_once_with(db, subscription.tariff_id)
    mock_enable.assert_awaited_once_with(db, user_id=user.id, subscription=subscription, tariff=tariff)


async def test_enable_value_error_maps_to_400(monkeypatch, user):
    """No price for the resolved charge period -> 400, not a 500."""
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    tariff = _tariff()

    async def fake_resolve(db, u, subscription_id):
        return subscription

    async def fake_enable(db, *, user_id, subscription, tariff):
        raise ValueError('no price for period')

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr('app.services.payment.platega.enable_platega_sbp_recurring', fake_enable)

    with pytest.raises(HTTPException) as exc_info:
        await route.enable_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == 'no price for period'


async def test_enable_runtime_error_maps_to_409(monkeypatch, user):
    """Platega API didn't return a transactionId -> 409, not a 500."""
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    tariff = _tariff()

    async def fake_resolve(db, u, subscription_id):
        return subscription

    async def fake_enable(db, *, user_id, subscription, tariff):
        raise RuntimeError('Platega did not return transactionId')

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr('app.services.payment.platega.enable_platega_sbp_recurring', fake_enable)

    with pytest.raises(HTTPException) as exc_info:
        await route.enable_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT


# --- get ------------------------------------------------------------------


async def test_get_404_when_no_subscription(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)

    async def fake_resolve(db, u, subscription_id):
        return None

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)

    with pytest.raises(HTTPException) as exc_info:
        await route.get_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


async def test_get_returns_none_status_without_active_record(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    db = AsyncMock()

    async def fake_resolve(resolve_db, u, subscription_id):
        assert resolve_db is db
        return subscription

    mock_get_active = AsyncMock(return_value=None)
    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        mock_get_active,
    )

    result = await route.get_platega_recurrent(user=user, db=db, subscription_id=None)

    assert result == {'status': 'none'}
    mock_get_active.assert_awaited_once_with(db, subscription.id)


async def test_get_returns_full_shape_for_active_record(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    next_charge = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    record = SimpleNamespace(
        status='ACTIVE',
        interval=3,
        amount_kopeks=19900,
        next_charge_at=next_charge,
        redirect_url='https://pay/existing',
    )

    async def fake_resolve(db, u, subscription_id):
        return subscription

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=record),
    )

    result = await route.get_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert result == {
        'status': 'ACTIVE',
        'interval': 3,
        'amount_kopeks': 19900,
        'next_charge_at': next_charge.isoformat(),
        'redirect_url': 'https://pay/existing',
    }


async def test_get_next_charge_at_none_serializes_to_none(monkeypatch, user):
    """A PENDING record has no next_charge_at yet (no callback received)."""
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    record = SimpleNamespace(
        status='PENDING',
        interval=3,
        amount_kopeks=19900,
        next_charge_at=None,
        redirect_url='https://pay/pending',
    )

    async def fake_resolve(db, u, subscription_id):
        return subscription

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr(
        'app.database.crud.platega_subscription.get_active_platega_subscription_by_subscription',
        AsyncMock(return_value=record),
    )

    result = await route.get_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert result['next_charge_at'] is None
    assert result['status'] == 'PENDING'


# --- cancel -----------------------------------------------------------------


async def test_cancel_404_when_no_subscription(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)

    async def fake_resolve(db, u, subscription_id):
        return None

    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)

    with pytest.raises(HTTPException) as exc_info:
        await route.cancel_platega_recurrent(user=user, db=AsyncMock(), subscription_id=None)

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


async def test_cancel_returns_cancelled_and_awaits_safe_helper(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)
    subscription = _Subscription()
    db = AsyncMock()

    async def fake_resolve(resolve_db, u, subscription_id):
        assert resolve_db is db
        return subscription

    mock_cancel = AsyncMock(return_value=None)
    monkeypatch.setattr(route, 'resolve_subscription', fake_resolve)
    monkeypatch.setattr(
        'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe',
        mock_cancel,
    )

    result = await route.cancel_platega_recurrent(user=user, db=db, subscription_id=None)

    assert result == {'status': 'cancelled'}
    mock_cancel.assert_awaited_once_with(db, subscription.id)


# --- purchase -----------------------------------------------------------------


async def test_purchase_403_when_gate_disabled(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=False)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await route.purchase_with_platega_recurrent(tariff_id=5, user=user, db=db)

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    db.execute.assert_not_awaited()


async def test_purchase_400_when_tariff_not_found(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc_info:
        await route.purchase_with_platega_recurrent(tariff_id=5, user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc_info.value.detail == 'Tariff not found'


async def test_purchase_happy_path_returns_redirect_and_subscription_id(monkeypatch, user):
    _configure_gate(monkeypatch, enabled=True)
    tariff = SimpleNamespace(id=5, name='Стандарт', is_daily=False, is_active=True)
    db = AsyncMock()

    mock_purchase = AsyncMock(
        return_value={
            'status': 'PENDING',
            'redirect_url': 'https://pay/x',
            'subscription_id': 91,
            'local_id': 1,
            'platega_subscription_id': 'ps',
        }
    )
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr('app.services.payment.platega.purchase_tariff_with_sbp_recurring', mock_purchase)

    result = await route.purchase_with_platega_recurrent(tariff_id=5, user=user, db=db)

    assert result == {'status': 'PENDING', 'redirect_url': 'https://pay/x', 'subscription_id': 91}
    mock_purchase.assert_awaited_once_with(db, user=user, tariff=tariff)


async def test_purchase_value_error_maps_to_400(monkeypatch, user):
    """Отказ сервиса (триал/чужой тариф/disabled) -> 400 с текстом причины."""
    _configure_gate(monkeypatch, enabled=True)
    tariff = SimpleNamespace(id=5, name='Стандарт', is_daily=False, is_active=True)

    async def fake_purchase(db, *, user, tariff):
        raise ValueError('СБП-оформление недоступно для триальной подписки — оплатите с баланса')

    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr('app.services.payment.platega.purchase_tariff_with_sbp_recurring', fake_purchase)

    with pytest.raises(HTTPException) as exc_info:
        await route.purchase_with_platega_recurrent(tariff_id=5, user=user, db=AsyncMock())

    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert 'триальной' in exc_info.value.detail
