"""Tests for get_extendable_subscriptions_by_user_id helper.

Verifies that promocode-extension-eligible subscriptions (ACTIVE, TRIAL,
LIMITED, EXPIRED) are returned, that DISABLED and PENDING are excluded,
and that ordering is ACTIVE > TRIAL > LIMITED > EXPIRED.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.database.crud.subscription import get_extendable_subscriptions_by_user_id
from app.database.models import SubscriptionStatus


def _make_db_returning(subscriptions):
    """Build an AsyncMock session whose execute().scalars().all() yields `subscriptions`."""
    db = AsyncMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=subscriptions)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    db.execute = AsyncMock(return_value=result)
    return db


async def test_returns_active_trial_limited_expired():
    """Helper returns subs in all four promocode-extension-eligible statuses."""
    subs = [
        SimpleNamespace(id=1, status=SubscriptionStatus.ACTIVE.value),
        SimpleNamespace(id=2, status=SubscriptionStatus.TRIAL.value),
        SimpleNamespace(id=3, status=SubscriptionStatus.LIMITED.value),
        SimpleNamespace(id=4, status=SubscriptionStatus.EXPIRED.value),
    ]
    db = _make_db_returning(subs)

    result = await get_extendable_subscriptions_by_user_id(db, user_id=42)

    assert [s.id for s in result] == [1, 2, 3, 4]
    db.execute.assert_awaited_once()


async def test_query_filters_statuses_correctly():
    """The SQL where-clause restricts to the four allowed statuses."""
    db = _make_db_returning([])

    await get_extendable_subscriptions_by_user_id(db, user_id=42)

    call_args = db.execute.call_args
    stmt = call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={'literal_binds': True}))
    assert SubscriptionStatus.ACTIVE.value in compiled
    assert SubscriptionStatus.TRIAL.value in compiled
    assert SubscriptionStatus.LIMITED.value in compiled
    assert SubscriptionStatus.EXPIRED.value in compiled
    assert SubscriptionStatus.DISABLED.value not in compiled
    assert SubscriptionStatus.PENDING.value not in compiled


async def test_returns_empty_when_no_subscriptions():
    """No matching subs → empty list, not None."""
    db = _make_db_returning([])

    result = await get_extendable_subscriptions_by_user_id(db, user_id=999)

    assert result == []
