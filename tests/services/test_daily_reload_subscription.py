"""Regression for the MissingGreenlet in daily_subscription_service.process_auto_resume.

After a status/squads commit (expire_on_commit) the eagerly-loaded user/tariff
relationships expire; a later subscription.user / .tariff access in the async
session lazy-loads → MissingGreenlet. The fix re-fetches the subscription with
selectinload(user, tariff) via _reload_daily_subscription instead of db.refresh,
so the relationships are loaded on the returned object.

These are contract tests over the mock AsyncSession (the repo has no real async DB
in tests); they guard that the helper re-fetches (a SELECT) and returns an object
whose user/tariff are directly accessible — i.e. it is not reverted to db.refresh.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import Select

from app.services.daily_subscription_service import DailySubscriptionService


async def test_reload_daily_subscription_refetches_and_returns_loaded_object():
    sub = SimpleNamespace(id=42, user=SimpleNamespace(id=1), tariff=SimpleNamespace(id=2, name='t'))

    result = MagicMock()
    result.scalar_one.return_value = sub

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    reloaded = await DailySubscriptionService()._reload_daily_subscription(db, 42)

    # Re-fetched via a SELECT (not db.refresh) …
    db.execute.assert_awaited_once()
    stmt = db.execute.await_args.args[0]
    assert isinstance(stmt, Select)
    # … and the relationships are present on the returned object.
    assert reloaded is sub
    assert reloaded.user.id == 1
    assert reloaded.tariff.id == 2


async def test_reload_query_eager_loads_user_and_tariff():
    """The re-fetch must carry selectinload options for user AND tariff."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one.return_value = SimpleNamespace(id=7)
    db.execute = AsyncMock(return_value=result)

    await DailySubscriptionService()._reload_daily_subscription(db, 7)

    stmt = db.execute.await_args.args[0]
    # loader options are attached to the Select; collect the relationship keys they
    # target (path elements mix Mapper + RelationshipProperty, so grab any .key).
    loaded_attrs: set[str] = set()
    for opt in getattr(stmt, '_with_options', ()):
        path = getattr(opt, 'path', None) or ()
        for el in path:
            key = getattr(el, 'key', None)
            if isinstance(key, str):
                loaded_attrs.add(key)
    assert {'user', 'tariff'} <= loaded_attrs
