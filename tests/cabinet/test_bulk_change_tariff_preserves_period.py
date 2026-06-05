"""Ground-truth regression test for the admin bulk "change tariff" action.

Telegram bug report #629889 (GoDFaTHeR): "Режим тарифы, без мультподписки —
если через массовые действия сменить тариф на тариф 2, пользователь получает
полную подписку (30 дней) тарифа два. По логике должно менять только тариф,
не сбрасывая срок."

This pins down what `_do_change_tariff` (the cabinet "Массовые действия" path)
actually does to `end_date`. The expectation is that changing the tariff is a
relabel of tariff/limits and MUST NOT reset the subscription period to a fresh
full term of the new tariff. The surrounding I/O (panel sync, tx, tariff lookup)
is mocked so we exercise only the field-mutation behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.cabinet.routes.admin_bulk_actions as bulk


def _tariff(
    *,
    tariff_id: int,
    name: str,
    traffic_limit_gb: int = 100,
    device_limit: int = 3,
    max_device_limit: int | None = None,
    allowed_squads: list[str] | None = None,
    is_trial_available: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tariff_id,
        name=name,
        traffic_limit_gb=traffic_limit_gb,
        device_limit=device_limit,
        max_device_limit=max_device_limit,
        allowed_squads=allowed_squads or [],
        is_trial_available=is_trial_available,
    )


def _subscription(
    *, end_date: datetime, tariff_id: int, is_trial: bool = False, status: str = 'active'
) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        user_id=7,
        status=status,
        is_trial=is_trial,
        end_date=end_date,
        tariff_id=tariff_id,
        traffic_limit_gb=50,
        traffic_used_gb=12.0,
        device_limit=3,
        connected_squads=['old-squad'],
        purchased_traffic_gb=10,
        traffic_reset_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=7, username='victim', subscriptions=[])


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=None)
    session.commit = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    return session


@pytest.mark.asyncio
async def test_change_tariff_preserves_remaining_period(db: AsyncMock) -> None:
    """A 5-days-left subscription keeps its 5 days — tariff swap must not refill to 30."""
    end_date = datetime.now(UTC) + timedelta(days=5)
    sub = _subscription(end_date=end_date, tariff_id=1)
    user = _user()

    old_tariff = _tariff(tariff_id=1, name='Tariff 1', traffic_limit_gb=50, device_limit=3)
    new_tariff = _tariff(tariff_id=2, name='Tariff 2', traffic_limit_gb=200, device_limit=5)

    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False
    fake_settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False

    with (
        patch.object(bulk, 'settings', fake_settings),
        patch.object(bulk, 'get_tariff_by_id', AsyncMock(return_value=old_tariff)),
        patch.object(bulk, '_sync_subscription_to_panel', AsyncMock(return_value={})),
        patch('app.database.crud.transaction.create_transaction', AsyncMock(return_value=None)),
    ):
        result = await bulk._do_change_tariff(
            db,
            user,
            SimpleNamespace(tariff_id=2),
            new_tariff,
            dry_run=False,
            sub_override=sub,
        )

    assert result.success is True
    # The crux: the subscription's end date is UNTOUCHED — no fresh 30-day term.
    assert sub.end_date == end_date
    # The tariff/limits ARE relabelled to the new tariff.
    assert sub.tariff_id == 2
    assert sub.traffic_limit_gb == 200


@pytest.mark.asyncio
async def test_change_tariff_does_not_extend_almost_expired_sub(db: AsyncMock) -> None:
    """An almost-expired sub stays almost-expired after a tariff change."""
    end_date = datetime.now(UTC) + timedelta(hours=6)
    sub = _subscription(end_date=end_date, tariff_id=1)
    user = _user()

    new_tariff = _tariff(tariff_id=2, name='Tariff 2')

    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False
    fake_settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False

    with (
        patch.object(bulk, 'settings', fake_settings),
        patch.object(bulk, 'get_tariff_by_id', AsyncMock(return_value=_tariff(tariff_id=1, name='Tariff 1'))),
        patch.object(bulk, '_sync_subscription_to_panel', AsyncMock(return_value={})),
        patch('app.database.crud.transaction.create_transaction', AsyncMock(return_value=None)),
    ):
        await bulk._do_change_tariff(
            db, user, SimpleNamespace(tariff_id=2), new_tariff, dry_run=False, sub_override=sub
        )

    assert sub.end_date == end_date


@pytest.mark.asyncio
async def test_change_tariff_keeps_trial_a_trial(db: AsyncMock) -> None:
    """Bug #629889: changing a TRIAL's tariff must NOT convert it to paid.

    Flipping is_trial=False on a 1-day trial turned it into a phantom paid sub
    that, once expired, got auto-extended to a full ~30-day period by
    try_auto_extend_expired_after_topup (which only renews is_trial=False subs).
    A trial must stay a trial across a tariff relabel and keep its 1-day term.
    """
    end_date = datetime.now(UTC) + timedelta(days=1)
    sub = _subscription(end_date=end_date, tariff_id=1, is_trial=True, status='trial')
    user = _user()

    # Target is a non-trial tariff — the exact case that used to trigger conversion.
    new_tariff = _tariff(tariff_id=2, name='Paid 30d', traffic_limit_gb=200, is_trial_available=False)

    fake_settings = MagicMock()
    fake_settings.is_multi_tariff_enabled.return_value = False
    fake_settings.RESET_TRAFFIC_ON_TARIFF_SWITCH = False

    with (
        patch.object(bulk, 'settings', fake_settings),
        patch.object(bulk, 'get_tariff_by_id', AsyncMock(return_value=_tariff(tariff_id=1, name='Trial'))),
        patch.object(bulk, '_sync_subscription_to_panel', AsyncMock(return_value={})),
        patch('app.database.crud.transaction.create_transaction', AsyncMock(return_value=None)),
    ):
        await bulk._do_change_tariff(
            db, user, SimpleNamespace(tariff_id=2), new_tariff, dry_run=False, sub_override=sub
        )

    # Still a trial -> auto-extend-after-topup skips it (is_trial gate), no 30-day grant.
    assert sub.is_trial is True
    assert sub.status == 'trial'
    # 1-day trial term preserved, never widened to the new tariff's period.
    assert sub.end_date == end_date
    # The relabel itself still happened.
    assert sub.tariff_id == 2
    assert sub.traffic_limit_gb == 200
