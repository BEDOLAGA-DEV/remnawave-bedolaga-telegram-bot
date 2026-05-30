from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.trial_invite_service as tis
from app.services.trial_invite_service import TrialInviteService


def _user(**kw):
    base = dict(id=10, referred_by_id=None, telegram_id=555, language='ru',
                trial_invite_bonus_days_used=0, trial_invite_rewarded_count=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _trial_sub(**kw):
    now = datetime.now(UTC)
    base = dict(id=99, user_id=1, is_trial=True, status='active',
                end_date=now + timedelta(days=2), remnawave_uuid='uuid-inv')
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def service():
    svc = TrialInviteService()
    svc._subscription_service = MagicMock()
    svc._subscription_service.create_remnawave_user = AsyncMock(return_value=None)
    svc._notify = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(tis.settings, 'TRIAL_INVITE_ENABLED', True, raising=False)
    monkeypatch.setattr(tis.settings.__class__, 'get_trial_invite_extend_days', lambda self: 3, raising=False)
    monkeypatch.setattr(tis.settings.__class__, 'get_trial_invite_max_extension_days', lambda self: 14, raising=False)
    yield


def _db(locked_sub=None):
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = locked_sub
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_reward_happy(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1, trial_invite_bonus_days_used=0)
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date

    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)

    assert inv_sub.end_date == old_end + timedelta(days=3)
    assert referrer.trial_invite_bonus_days_used == 3
    assert referrer.trial_invite_rewarded_count == 1
    service._subscription_service.create_remnawave_user.assert_awaited_once()
    service._notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_referrer_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=None)
    db = _db()
    get_user = AsyncMock()
    monkeypatch.setattr(tis, 'get_user_by_id', get_user)
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    get_user.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_invite_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=10)
    db = _db()
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=invitee))
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_referrer_not_on_trial_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1)
    db = _db(locked_sub=None)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    service._subscription_service.create_remnawave_user.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_exhausted_noop(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1, trial_invite_bonus_days_used=14)
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    assert inv_sub.end_date == old_end
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_partial_grant(service, monkeypatch):
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1, trial_invite_bonus_days_used=12)
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    assert inv_sub.end_date == old_end + timedelta(days=2)
    assert referrer.trial_invite_bonus_days_used == 14


@pytest.mark.asyncio
async def test_disabled_flag_noop(service, monkeypatch):
    monkeypatch.setattr(tis.settings, 'TRIAL_INVITE_ENABLED', False, raising=False)
    invitee = _user(id=10, referred_by_id=1)
    get_user = AsyncMock()
    monkeypatch.setattr(tis, 'get_user_by_id', get_user)
    db = _db()
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    get_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_failure_keeps_time_enqueues(service, monkeypatch):
    service._subscription_service.create_remnawave_user = AsyncMock(side_effect=RuntimeError('panel down'))
    enqueue = MagicMock()
    monkeypatch.setattr(tis.remnawave_retry_queue, 'enqueue', enqueue)
    invitee = _user(id=10, referred_by_id=1)
    referrer = _user(id=1)
    inv_sub = _trial_sub(user_id=1)
    db = _db(locked_sub=inv_sub)
    monkeypatch.setattr(tis, 'get_user_by_id', AsyncMock(return_value=referrer))
    old_end = inv_sub.end_date
    await service.reward_inviter_on_trial_activation(db, invitee, bot=None)
    assert inv_sub.end_date == old_end + timedelta(days=3)
    db.commit.assert_awaited()
    enqueue.assert_called_once()
