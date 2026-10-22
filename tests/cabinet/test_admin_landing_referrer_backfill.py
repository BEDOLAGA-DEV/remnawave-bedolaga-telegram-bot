import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.cabinet.routes.admin_landings import _backfill_landing_referrals


@pytest.mark.asyncio
async def test_backfill_landing_referrals_none_referrer() -> None:
    db = AsyncMock()
    # If referrer_id is None, it should return early without executing any query
    await _backfill_landing_referrals(db, landing_id=1, referrer_id=None)
    db.execute.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_landing_referrals_with_referrer() -> None:
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    await _backfill_landing_referrals(db, landing_id=42, referrer_id=99)

    # Verify db.execute was called to update User referred_by_id
    db.execute.assert_called_once()
    # Verify commit was executed to persist the changes
    db.commit.assert_called_once()

    # Inspect the query argument to verify structure
    args, kwargs = db.execute.call_args
    query = args[0]
    
    # Simple check that it is an Update statement referencing users
    assert "UPDATE users" in str(query) or "update" in str(type(query)).lower()
