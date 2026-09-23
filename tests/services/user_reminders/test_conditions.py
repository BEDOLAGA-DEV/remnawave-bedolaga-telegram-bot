"""Условия напоминаний: SQL-вход и проверка одного человека обязаны отвечать одинаково.

Бот отбирает кандидатов SQL-запросом, кабинет проверяет одного человека в Python.
Разойдутся — человек получит в боте напоминание, которого не видит в кабинете, или наоборот.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.database.models import Base, Subscription, User
from app.services.user_reminders.conditions import (
    LOW_BALANCE_THRESHOLD_KOPEKS,
    condition_clauses,
    matches,
    parse_conditions,
)
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, **kw) -> User:
    base = dict(
        id=uid,
        telegram_id=None,
        email=None,
        password_hash=None,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=0,
        created_at=NOW - timedelta(days=30),
        last_activity=NOW - timedelta(days=1),
        cabinet_last_login=None,
    )
    base.update(kw)
    return User(**base)


def _sub(sid: int, uid: int, *, status='active', trial=False, days=10, tariff_id=None) -> Subscription:
    return Subscription(
        id=sid,
        user_id=uid,
        remnawave_short_id=f's{sid}',
        status=status,
        is_trial=trial,
        start_date=NOW - timedelta(days=40),
        end_date=NOW + timedelta(days=days),
        tariff_id=tariff_id,
    )


# (conditions, [users], [subs], ожидаемые id)
CASES = {
    'telegram_only': (
        {'auth': 'telegram_only'},
        [
            _user(1, telegram_id=11),
            _user(2, telegram_id=12, email='a@x', password_hash='h'),
            _user(3, email='b@x', password_hash='h'),
        ],
        [],
        {1},
    ),
    'email_only': (
        {'auth': 'email_only'},
        [
            _user(1, telegram_id=11),
            _user(2, email='b@x', password_hash='h'),
            _user(3, email='c@x', password_hash='h', google_id='g'),
        ],
        [],
        {2},
    ),
    'email_without_password_is_not_a_method': (
        {'auth': 'telegram_only'},
        [_user(1, telegram_id=11, email='a@x', password_hash=None)],
        [],
        {1},
    ),
    'empty_strings_are_not_methods': (
        {'auth': 'single_method'},
        [_user(1, telegram_id=11, google_id='', email='', password_hash='h')],
        [],
        {1},
    ),
    'single_method_any_kind': (
        {'auth': 'single_method'},
        [_user(1, telegram_id=11), _user(2, google_id='g'), _user(3, telegram_id=13, vk_id='v'), _user(4)],
        [],
        {1, 2},
    ),
    'deleted_and_blocked_never_match': (
        {},
        [
            _user(1, telegram_id=11),
            _user(2, telegram_id=12, status='deleted'),
            _user(3, telegram_id=13, status='blocked'),
        ],
        [],
        {1},
    ),
    'active_segment': (
        {'subscription': {'segment': 'active'}},
        [_user(1), _user(2), _user(3), _user(4)],
        [
            _sub(10, 1),
            _sub(20, 2, trial=True, status='trial'),
            _sub(30, 3, status='expired', days=-5),
            _sub(40, 4, days=-1),
        ],
        {1},
    ),
    'trial_segment': (
        {'subscription': {'segment': 'trial'}},
        [_user(1), _user(2)],
        [_sub(10, 1, trial=True, status='trial'), _sub(20, 2, trial=True, status='expired', days=-3)],
        {1},
    ),
    'expiring_segment': (
        {'subscription': {'segment': 'expiring', 'days': 3}},
        [_user(1), _user(2), _user(3)],
        [_sub(10, 1, days=2), _sub(20, 2, days=10), _sub(30, 3, days=-1)],
        {1},
    ),
    'expired_vs_none_vs_pending': (
        {'subscription': {'segment': 'expired'}},
        [_user(1), _user(2), _user(3), _user(4)],
        [_sub(10, 1, status='expired', days=-5), _sub(30, 3, status='pending', days=30), _sub(40, 4)],
        {1},
    ),
    'none_segment': (
        {'subscription': {'segment': 'none'}},
        [_user(1), _user(2), _user(3)],
        [_sub(10, 1, status='expired', days=-5), _sub(30, 3, status='pending', days=30)],
        {2, 3},
    ),
    'multi_tariff_any_subscription_counts': (
        {'subscription': {'segment': 'active'}},
        [_user(1)],
        [_sub(10, 1, status='expired', days=-5), _sub(11, 1)],
        {1},
    ),
    'tariff_segment': (
        {'subscription': {'segment': 'tariff', 'tariff_id': 7}},
        [_user(1), _user(2)],
        [_sub(10, 1, tariff_id=7), _sub(20, 2, tariff_id=8)],
        {1},
    ),
    'low_balance': (
        {'subscription': {'segment': 'low_balance'}},
        [
            _user(1, balance_kopeks=5000),
            _user(2, balance_kopeks=0),
            _user(3, balance_kopeks=LOW_BALANCE_THRESHOLD_KOPEKS),
        ],
        [],
        {1},
    ),
    'registered_days_min': (
        {'registered_days_min': 3},
        [_user(1, created_at=NOW - timedelta(days=5)), _user(2, created_at=NOW - timedelta(days=1))],
        [],
        {1},
    ),
    'inactive_uses_freshest_of_bot_and_cabinet': (
        {'inactive_days_min': 7},
        [
            _user(1, last_activity=NOW - timedelta(days=10), cabinet_last_login=None),
            _user(2, last_activity=NOW - timedelta(days=10), cabinet_last_login=NOW - timedelta(days=1)),
            _user(3, last_activity=None, cabinet_last_login=None),
        ],
        [],
        {1, 3},
    ),
    'conditions_are_anded': (
        {'auth': 'telegram_only', 'subscription': {'segment': 'active'}, 'registered_days_min': 3},
        [_user(1, telegram_id=11), _user(2, telegram_id=12), _user(3, telegram_id=13, created_at=NOW)],
        [_sub(10, 1), _sub(30, 3)],
        {1},
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(CASES))
async def test_sql_and_python_agree(monkeypatch, name):
    from sqlalchemy import update

    raw, users, subs, expected = CASES[name]
    conditions = parse_conditions(raw)
    async with memory_session(monkeypatch, TABLES) as db:
        # Capture scenario values before insertion (Column defaults will overwrite them in DB).
        user_timestamps = {u.id: (u.last_activity, u.cabinet_last_login) for u in users}

        db.add_all(users)
        await db.flush()
        db.add_all(subs)
        await db.commit()

        # Force the DB rows to match scenario values (override Column defaults that fired on insert).
        for user_id, (last_activity, cabinet_last_login) in user_timestamps.items():
            await db.execute(
                update(User)
                .where(User.id == user_id)
                .values(last_activity=last_activity, cabinet_last_login=cabinet_last_login)
            )
        await db.commit()

        # Reload users and subs from DB so both SQL and Python see identical data.
        users = (await db.execute(select(User).execution_options(populate_existing=True))).scalars().all()
        subs = (await db.execute(select(Subscription).execution_options(populate_existing=True))).scalars().all()

        sql_ids = set((await db.execute(select(User.id).where(*condition_clauses(conditions, now=NOW)))).scalars())
        py_ids = {
            user.id for user in users if matches(user, [s for s in subs if s.user_id == user.id], conditions, now=NOW)
        }

    assert sql_ids == expected, 'SQL-вход'
    assert py_ids == expected, 'проверка одного человека'


def test_python_auth_matches_compute_auth_methods():
    from app.services.account_merge_service import compute_auth_methods

    user = _user(1, telegram_id=11, email='a@x', password_hash='h', google_id='g')
    assert set(compute_auth_methods(user)) == {'telegram', 'email', 'google'}
    assert not matches(user, [], parse_conditions({'auth': 'single_method'}), now=NOW)


def test_low_balance_threshold_is_the_broadcast_one():
    from app.handlers.admin.messages import (
        LOW_BALANCE_THRESHOLD_KOPEKS as BROADCAST_THRESHOLD,
    )

    assert LOW_BALANCE_THRESHOLD_KOPEKS == BROADCAST_THRESHOLD


@pytest.mark.parametrize(
    'raw',
    [
        {'auth': 'nobody'},
        {'subscription': {'segment': 'expiring'}},
        {'subscription': {'segment': 'tariff'}},
        {'registered_days_min': -1},
        {'inactive_days_min': 4000},
        {'unknown': 1},
    ],
)
def test_invalid_conditions_are_rejected(raw):
    with pytest.raises(ValidationError):
        parse_conditions(raw)
