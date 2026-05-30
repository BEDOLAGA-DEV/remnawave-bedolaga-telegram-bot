from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.birthday_service as bs
from app.services.birthday_service import BirthdayService
from app.services import birthday_settings_service


class _Birthdate:
    def __init__(self, day, month, year=None):
        self.day, self.month, self.year = day, month, year


def _user(**kw):
    base = dict(
        id=1, telegram_id=555, birth_date=None,
        birthday_synced_at=None, birthday_changed_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _ctx:
    def __init__(self, db):
        self._db = db
    async def __aenter__(self):
        return self._db
    async def __aexit__(self, *a):
        return False


@pytest.fixture
def service():
    svc = BirthdayService()
    svc._bot = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_sync_stores_birthdate(service, monkeypatch):
    user = _user()
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(return_value=SimpleNamespace(birthdate=_Birthdate(15, 6, 1990)))

    await service.sync_user_birthday(1, 555)

    assert user.birth_date == date(1990, 6, 15)
    assert user.birthday_synced_at is not None
    assert user.birthday_changed_at is not None


@pytest.mark.asyncio
async def test_sync_none_birthdate_keeps_existing(service, monkeypatch):
    user = _user(birth_date=date(1990, 6, 15))
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(return_value=SimpleNamespace(birthdate=None))

    await service.sync_user_birthday(1, 555)

    assert user.birth_date == date(1990, 6, 15)
    assert user.birthday_synced_at is not None


@pytest.mark.asyncio
async def test_sync_change_updates_changed_at(service, monkeypatch):
    user = _user(birth_date=date(1990, 6, 15), birthday_changed_at=datetime(2020, 1, 1, tzinfo=UTC))
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(return_value=SimpleNamespace(birthdate=_Birthdate(3, 3, 1992)))

    await service.sync_user_birthday(1, 555)

    assert user.birth_date == date(1992, 3, 3)
    assert user.birthday_changed_at > datetime(2020, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sync_get_chat_failure_is_swallowed(service, monkeypatch):
    user = _user()
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    db.commit = AsyncMock()
    monkeypatch.setattr(bs, 'AsyncSessionLocal', lambda: _ctx(db))
    service._bot.get_chat = AsyncMock(side_effect=RuntimeError('flood'))

    await service.sync_user_birthday(1, 555)


# ---------------------------------------------------------------------------
# Grant / scheduler tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr(bs.BirthdaySettingsService, 'is_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_reward_type', classmethod(lambda cls: 'balance'))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_reward_amount', classmethod(lambda cls: 10000))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_min_account_age_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_dob_stable_days', classmethod(lambda cls: 7))
    monkeypatch.setattr(bs.BirthdaySettingsService, 'get_subscription_days_fallback', classmethod(lambda cls: 'balance'))
    yield


def _bday_user(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=1, telegram_id=555, language='ru',
        birth_date=date(1990, now.month, now.day),
        birthday_changed_at=now - timedelta(days=400),
        created_at=now - timedelta(days=400),
        last_birthday_reward_year=None,
        status='active',
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_grant_balance_reward(service, monkeypatch):
    user = _bday_user()
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_awaited_once()
    assert user.last_birthday_reward_year == datetime.now(UTC).year
    service._notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_grant_skips_already_rewarded_this_year(service, monkeypatch):
    user = _bday_user(last_birthday_reward_year=datetime.now(UTC).year)
    db = MagicMock(); db.commit = AsyncMock(); db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_skips_young_account(service, monkeypatch):
    user = _bday_user(created_at=datetime.now(UTC) - timedelta(days=2))
    db = MagicMock(); db.commit = AsyncMock(); db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_skips_recently_changed_dob(service, monkeypatch):
    user = _bday_user(birthday_changed_at=datetime.now(UTC) - timedelta(days=2))
    db = MagicMock(); db.commit = AsyncMock(); db.rollback = AsyncMock()
    monkeypatch.setattr(service, '_select_birthday_users', AsyncMock(return_value=[user]))
    add_balance = AsyncMock()
    monkeypatch.setattr(bs, 'add_user_balance', add_balance)
    service._notify = AsyncMock()

    await service._grant_birthday_rewards(db)

    add_balance.assert_not_awaited()
