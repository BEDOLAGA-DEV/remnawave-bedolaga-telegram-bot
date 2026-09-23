"""Проход бота: частота, лимиты, тихие часы, остановка при выполненном условии."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, User, UserReminder, UserReminderState
from app.services.user_reminders import dispatcher
from app.services.user_reminders.dispatcher import is_quiet_time, run_reminder_pass
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)  # 12:00 UTC — не тихие часы при TIMEZONE=UTC
TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture(autouse=True)
def reminder_settings(monkeypatch):
    monkeypatch.setattr(settings, 'USER_REMINDERS_QUIET_HOURS_START', 21)
    monkeypatch.setattr(settings, 'USER_REMINDERS_QUIET_HOURS_END', 10)
    monkeypatch.setattr(settings, 'USER_REMINDERS_DAILY_LIMIT_ENABLED', True)
    monkeypatch.setattr(settings, 'USER_REMINDERS_MAX_PER_PASS', 500)
    monkeypatch.setattr(dispatcher, 'get_local_timezone', lambda: ZoneInfo('UTC'))


class Recorder:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[int, int]] = []

    async def __call__(self, user, reminder, bot) -> bool:
        self.calls.append((reminder.id, user.id))
        return self.ok


async def _no_sleep(_):
    return None


def _user(uid: int, **kw) -> User:
    base = dict(
        id=uid,
        telegram_id=1000 + uid,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
        created_at=NOW - timedelta(days=30),
    )
    base.update(kw)
    return User(**base)


def _reminder(rid: int, **kw) -> UserReminder:
    base = dict(
        id=rid,
        name=f'r{rid}',
        channels='bot',
        category='service',
        conditions={},
        repeat_every_days=7,
        max_sends=2,
        texts={'ru': {'title': 't', 'body': 'b'}},
        button_kind='none',
        is_active=True,
    )
    base.update(kw)
    return UserReminder(**base)


async def _seed(db, users, reminders, states=()):
    db.add_all(users)
    db.add_all(reminders)
    await db.flush()
    db.add_all(states)
    await db.commit()


async def _pass(db, deliver, now=NOW):
    return await run_reminder_pass(db, bot=object(), now=now, deliver=deliver, sleep=_no_sleep)


@pytest.mark.parametrize(
    ('hour', 'quiet'), [(9, True), (10, False), (15, False), (20, False), (21, True), (23, True), (3, True)]
)
def test_quiet_hours_wrap_midnight(hour, quiet):
    now = datetime(2026, 9, 22, hour, 30, tzinfo=UTC)
    assert is_quiet_time(now, start_hour=21, end_hour=10, tz=ZoneInfo('UTC')) is quiet


def test_equal_hours_mean_no_quiet_time():
    assert is_quiet_time(NOW, start_hour=0, end_hour=0, tz=ZoneInfo('UTC')) is False


@pytest.mark.asyncio
async def test_sends_once_then_waits_for_repeat_window(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [_user(1)], [_reminder(1)])
        deliver = Recorder()

        assert (await _pass(db, deliver)).sent == 1
        assert (await _pass(db, deliver, NOW + timedelta(days=3))).sent == 0
        monkeypatch.setattr(settings, 'USER_REMINDERS_DAILY_LIMIT_ENABLED', False)
        assert (await _pass(db, deliver, NOW + timedelta(days=8))).sent == 1
        assert (await _pass(db, deliver, NOW + timedelta(days=30))).sent == 0, 'max_sends=2 исчерпан'
        state = (await db.execute(select(UserReminderState))).scalar_one()
        assert state.sends_count == 2


@pytest.mark.asyncio
async def test_quiet_hours_send_nothing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [_user(1)], [_reminder(1)])
        deliver = Recorder()
        assert (await _pass(db, deliver, datetime(2026, 9, 22, 23, 0, tzinfo=UTC))).sent == 0
        assert deliver.calls == []


@pytest.mark.asyncio
async def test_one_reminder_per_person_per_pass_and_daily_limit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [_user(1)], [_reminder(1), _reminder(2)])
        deliver = Recorder()

        await _pass(db, deliver)
        await _pass(db, deliver, NOW + timedelta(hours=2))
        assert deliver.calls == [(1, 1)], 'второе напоминание — не раньше чем через сутки'

        await _pass(db, deliver, NOW + timedelta(hours=25))
        assert deliver.calls == [(1, 1), (2, 1)]


@pytest.mark.asyncio
async def test_resolved_condition_stops_reminders(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [_user(1)], [_reminder(1, conditions={'auth': 'telegram_only'}, repeat_every_days=1)])
        deliver = Recorder()
        await _pass(db, deliver)

        user = await db.get(User, 1)
        user.google_id = 'linked'
        await db.commit()

        assert (await _pass(db, deliver, NOW + timedelta(days=3))).sent == 0


@pytest.mark.asyncio
async def test_failed_send_does_not_retry_every_pass(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [_user(1)], [_reminder(1)])
        deliver = Recorder(ok=False)

        assert (await _pass(db, deliver)).failed == 1
        assert (await _pass(db, deliver, NOW + timedelta(minutes=15))).failed == 0
        state = (await db.execute(select(UserReminderState))).scalar_one()
        assert (state.sends_count, state.last_success_at) == (0, None)


@pytest.mark.asyncio
async def test_users_without_telegram_and_cabinet_only_reminders_are_skipped(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            db,
            [_user(1, telegram_id=None, email='e@x', password_hash='h'), _user(2)],
            [_reminder(1, channels='cabinet'), _reminder(2)],
        )
        deliver = Recorder()
        await _pass(db, deliver)
        assert deliver.calls == [(2, 2)]


@pytest.mark.asyncio
async def test_marketing_respects_promo_opt_out_without_starving_the_queue(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            db,
            [_user(1, notification_settings={'promo_offers_enabled': False}), _user(2)],
            [_reminder(1, category='marketing')],
        )
        deliver = Recorder()
        result = await _pass(db, deliver)
        assert deliver.calls == [(1, 2)]
        assert result.skipped == 1
        # Отписанный отмечен попыткой — следующий проход его не перебирает.
        assert (await _pass(db, deliver, NOW + timedelta(minutes=15))).skipped == 0


@pytest.mark.asyncio
async def test_opted_out_prefix_larger_than_budget_does_not_block_later_people(monkeypatch):
    """Ревью PR #3280: голова очереди из отписанных закрывала весь проход.

    Выборка берёт столько кандидатов, сколько осталось бюджета; отписанные его не
    тратят. Без дочитывания следующей порции люди дальше по списку не получали
    ничего, а после окна повтора та же голова снова всё закрывала.
    """
    monkeypatch.setattr(settings, 'USER_REMINDERS_MAX_PER_PASS', 2)
    opted_out = {'promo_offers_enabled': False}
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            db,
            [_user(i, notification_settings=opted_out) for i in (1, 2, 3)] + [_user(i) for i in (4, 5, 6)],
            [_reminder(1, category='marketing')],
        )
        deliver = Recorder()
        result = await _pass(db, deliver)

        assert deliver.calls == [(1, 4), (1, 5)]
        assert result.sent == 2
        assert result.skipped == 3


@pytest.mark.asyncio
async def test_pass_ceiling(monkeypatch):
    monkeypatch.setattr(settings, 'USER_REMINDERS_MAX_PER_PASS', 2)
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [_user(i) for i in range(1, 6)], [_reminder(1)])
        deliver = Recorder()
        assert (await _pass(db, deliver)).sent == 2
        assert (await _pass(db, deliver)).sent == 2


@pytest.mark.asyncio
async def test_inactive_reminder_is_silent(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [_user(1)], [_reminder(1, is_active=False)])
        deliver = Recorder()
        await _pass(db, deliver)
        assert deliver.calls == []


@pytest.mark.asyncio
async def test_broken_texts_reminder_is_skipped_not_fatal(monkeypatch):
    """texts без 'ru' — render_bot_message кинул бы KeyError на каждом кандидате.

    Проход должен пропустить это напоминание целиком (как с битыми условиями)
    и всё равно доставить валидное.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            db,
            [_user(1)],
            [
                _reminder(1, texts={'en': {'title': 't', 'body': 'b'}}),
                _reminder(2),
            ],
        )
        deliver = Recorder()
        result = await _pass(db, deliver)
        assert deliver.calls == [(2, 1)]
        assert result.sent == 1
