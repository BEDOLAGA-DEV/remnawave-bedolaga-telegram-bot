"""Два независимых входа не могут привязать разных партнёров одному пользователю."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.database.models import User
from app.services import referral_service
from tests.fixtures.postgres_db import postgres_sessions


pytestmark = pytest.mark.postgres


async def test_two_referrers_have_one_winner(postgres_database, monkeypatch):
    async with postgres_sessions(postgres_database, (User.__table__,)) as (left, right):
        target = User(telegram_id=91001, language='ru')
        first = User(telegram_id=91002, language='ru', referral_code='masked-first')
        second = User(telegram_id=91003, language='ru', referral_code='masked-second')
        left.add_all([target, first, second])
        await left.commit()
        # Both sessions have read the pre-attachment NULL state.
        other_copy = await right.get(User, target.id)
        assert other_copy.referred_by_id is None
        registration = AsyncMock()
        monkeypatch.setattr(referral_service, 'process_referral_registration', registration)
        monkeypatch.setattr(referral_service, 'clear_pending_referral', AsyncMock())

        outcomes = await asyncio.gather(
            referral_service.attach_referrer_if_missing(
                left, target, referral_code='masked-first', bot=object(), source='test'
            ),
            referral_service.attach_referrer_if_missing(
                right, other_copy, referral_code='masked-second', bot=object(), source='test'
            ),
        )
        winners = [value for value in outcomes if value is not None]
        assert len(winners) == 1
        assert winners[0] in {first.id, second.id}
        await left.refresh(target)
        assert target.referred_by_id == winners[0]
        registration.assert_awaited_once()
        assert registration.await_args.args[1:3] == (target.id, winners[0])
