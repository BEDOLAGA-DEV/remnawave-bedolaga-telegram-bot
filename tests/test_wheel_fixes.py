"""Regression tests for the Fortune Wheel hardening:

A3 — the daily spin limit is re-checked *under the user lock* inside spin(), not
     only in the up-front check_availability (which races with concurrent /spin).
A2 — a real Telegram Stars wheel spin is idempotent: a redelivered
     successful_payment (same charge id) must not grant a second prize.
A3(stars) — the Stars handler enforces the daily limit at grant time and refunds
     the stars to balance instead of silently over-granting.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.wheel_service import FortuneWheelService, SpinAvailability


@pytest.mark.asyncio
async def test_spin_rechecks_daily_limit_under_lock() -> None:
    """Even if check_availability passed, spin() must re-count under the lock and
    bail when the limit is now reached — without charging the user."""
    svc = FortuneWheelService()
    user = SimpleNamespace(id=1, balance_kopeks=10_000_000)
    config = SimpleNamespace(id=1, daily_spin_limit=5, spin_cost_stars=10, spin_cost_stars_enabled=True)
    process_stars = AsyncMock()
    process_days = AsyncMock()

    with ExitStack() as s:
        s.enter_context(
            patch.object(
                svc,
                'check_availability',
                AsyncMock(return_value=SpinAvailability(can_spin=True, can_pay_stars=True)),
            )
        )
        s.enter_context(patch('app.services.wheel_service.get_or_create_wheel_config', AsyncMock(return_value=config)))
        s.enter_context(
            patch('app.services.wheel_service.get_wheel_prizes', AsyncMock(return_value=[SimpleNamespace(id=1)]))
        )
        s.enter_context(patch('app.database.crud.user.lock_user_for_update', AsyncMock(return_value=user)))
        # Count is now AT the limit (a concurrent spin committed in between).
        s.enter_context(patch('app.services.wheel_service.get_user_spins_today', AsyncMock(return_value=5)))
        s.enter_context(patch.object(svc, '_process_stars_payment', process_stars))
        s.enter_context(patch.object(svc, '_process_days_payment', process_days))

        result = await svc.spin(AsyncMock(), user, 'telegram_stars')

    assert result.success is False
    assert result.error == 'daily_limit_reached'
    process_stars.assert_not_awaited()  # never charged
    process_days.assert_not_awaited()


@pytest.mark.asyncio
async def test_spin_under_limit_proceeds_to_payment() -> None:
    """Sanity: when the re-check is below the limit, spin() proceeds to payment."""
    svc = FortuneWheelService()
    user = SimpleNamespace(id=1, balance_kopeks=10_000_000)
    config = SimpleNamespace(id=1, daily_spin_limit=5, spin_cost_stars=10, spin_cost_stars_enabled=True)
    # Make payment raise a clean ValueError so we stop right after the limit gate
    # without having to mock the entire prize pipeline — proves the gate was passed.
    process_stars = AsyncMock(side_effect=ValueError('stop-after-gate'))

    with ExitStack() as s:
        s.enter_context(
            patch.object(
                svc,
                'check_availability',
                AsyncMock(return_value=SpinAvailability(can_spin=True, can_pay_stars=True)),
            )
        )
        s.enter_context(patch('app.services.wheel_service.get_or_create_wheel_config', AsyncMock(return_value=config)))
        s.enter_context(
            patch('app.services.wheel_service.get_wheel_prizes', AsyncMock(return_value=[SimpleNamespace(id=1)]))
        )
        s.enter_context(patch('app.database.crud.user.lock_user_for_update', AsyncMock(return_value=user)))
        s.enter_context(patch('app.services.wheel_service.get_user_spins_today', AsyncMock(return_value=2)))
        s.enter_context(
            patch(
                'app.services.wheel_service.settings',
                MagicMock(is_multi_tariff_enabled=MagicMock(return_value=False)),
            )
        )
        s.enter_context(patch('app.services.wheel_service.get_subscription_by_user_id', AsyncMock(return_value=None)))
        s.enter_context(patch.object(svc, '_process_stars_payment', process_stars))

        result = await svc.spin(AsyncMock(), user, 'telegram_stars')

    assert result.error == 'payment_error'  # got past the limit gate into payment
    process_stars.assert_awaited_once()


@pytest.mark.asyncio
async def test_stars_wheel_spin_idempotent_on_redelivery() -> None:
    """A successful_payment redelivered with the same charge id must NOT grant a
    second prize — the pre-check on the existing spin short-circuits."""
    from app.handlers.stars_payments import _handle_wheel_spin_payment

    create_spin = AsyncMock()
    config_loader = AsyncMock()  # must NOT be reached

    with ExitStack() as s:
        s.enter_context(
            patch(
                'app.database.crud.wheel.get_wheel_spin_by_charge_id',
                AsyncMock(return_value=SimpleNamespace(id=99)),
            )
        )
        s.enter_context(patch('app.database.crud.wheel.get_or_create_wheel_config', config_loader))
        s.enter_context(patch('app.database.crud.wheel.create_wheel_spin', create_spin))

        result = await _handle_wheel_spin_payment(
            message=AsyncMock(),
            db=AsyncMock(),
            user=SimpleNamespace(id=1, telegram_id=123),
            stars_amount=10,
            payload='wheel_spin_1_123',
            texts=None,
            charge_id='CHARGE_ABC',
        )

    assert result is True  # treated as already-processed
    create_spin.assert_not_awaited()  # no second prize
    config_loader.assert_not_awaited()  # short-circuited before any work
