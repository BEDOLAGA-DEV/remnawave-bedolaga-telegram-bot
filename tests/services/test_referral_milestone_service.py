from unittest.mock import AsyncMock, MagicMock

import pytest

import app.database.crud.referral as ref_crud


@pytest.mark.asyncio
async def test_count_paid_referrals_returns_scalar():
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = 5
    db.execute = AsyncMock(return_value=result)
    n = await ref_crud.count_paid_referrals(db, 42)
    assert n == 5
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_count_paid_referrals_none_to_zero():
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = None
    db.execute = AsyncMock(return_value=result)
    assert await ref_crud.count_paid_referrals(db, 42) == 0
