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


from types import SimpleNamespace

import app.services.referral_milestone_service as ms


def _milestone(mid, threshold, reward_type='balance', reward_value=5000):
    return SimpleNamespace(id=mid, threshold=threshold, reward_type=reward_type,
                           reward_value=reward_value, title={'ru': f'M{threshold}'})


@pytest.fixture
def service():
    svc = ms.ReferralMilestoneService()
    svc._notify = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(ms.settings, 'REFERRAL_MILESTONES_ENABLED', True, raising=False)
    yield


def _db():
    db = MagicMock()
    db.commit = AsyncMock(); db.rollback = AsyncMock(); db.add = MagicMock(); db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_disabled_returns_empty(service, monkeypatch):
    monkeypatch.setattr(ms.settings, 'REFERRAL_MILESTONES_ENABLED', False, raising=False)
    granted = await service.reward_milestones(_db(), 1, bot=None)
    assert granted == []


@pytest.mark.asyncio
async def test_grants_reached_unclaimed(service, monkeypatch):
    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=5))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 1), _milestone(2, 3), _milestone(3, 5), _milestone(4, 10)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value=set()))
    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr(ms, 'add_user_balance', add_balance)

    granted = await service.reward_milestones(db, 1, bot=None)

    assert len(granted) == 3
    assert add_balance.await_count == 3
    # balance reward must be applied with commit=False (atomic with the claim)
    assert all(call.kwargs.get('commit') is False for call in add_balance.await_args_list)


@pytest.mark.asyncio
async def test_skips_already_claimed(service, monkeypatch):
    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=5))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 1), _milestone(3, 5)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value={1}))
    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr(ms, 'add_user_balance', add_balance)

    granted = await service.reward_milestones(db, 1, bot=None)
    assert len(granted) == 1
    add_balance.assert_awaited_once()


@pytest.mark.asyncio
async def test_promo_group_reward(service, monkeypatch):
    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=10))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 10, reward_type='promo_group', reward_value=7)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value=set()))
    add_pg = AsyncMock()
    monkeypatch.setattr(ms, 'add_user_to_promo_group', add_pg)

    granted = await service.reward_milestones(db, 1, bot=None)
    assert len(granted) == 1
    # promo_group grant must be commit=False (atomic with the claim, service owns commit)
    add_pg.assert_awaited_once_with(db, 1, 7, assigned_by='system', commit=False)


@pytest.mark.asyncio
async def test_claim_race_integrity_error_skips(service, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    db.flush = AsyncMock(side_effect=IntegrityError('dup', None, Exception()))
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=5))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 1)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value=set()))
    add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr(ms, 'add_user_balance', add_balance)

    granted = await service.reward_milestones(db, 1, bot=None)

    # claim insert raced (already claimed by concurrent tx) → skip, no double reward
    assert granted == []
    add_balance.assert_not_awaited()
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_invalid_promo_group_skips_without_breaking(service, monkeypatch):
    referrer = SimpleNamespace(id=1, telegram_id=10, language='ru')
    db = _db()
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=referrer))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=10))
    monkeypatch.setattr(ms.milestone_crud, 'list_active',
                        AsyncMock(return_value=[_milestone(1, 10, reward_type='promo_group', reward_value=999)]))
    monkeypatch.setattr(ms.milestone_crud, 'get_claimed_milestone_ids', AsyncMock(return_value=set()))
    monkeypatch.setattr(ms, 'add_user_to_promo_group', AsyncMock(side_effect=RuntimeError('no such promo group')))

    granted = await service.reward_milestones(db, 1, bot=None)

    assert granted == []  # misconfigured milestone skipped, not granted
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_no_paid_referrals_noop(service, monkeypatch):
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(id=1, telegram_id=10, language='ru')))
    monkeypatch.setattr(ms.ref_crud, 'count_paid_referrals', AsyncMock(return_value=0))
    la = AsyncMock()
    monkeypatch.setattr(ms.milestone_crud, 'list_active', la)
    granted = await service.reward_milestones(_db(), 1, bot=None)
    assert granted == []
    la.assert_not_awaited()
