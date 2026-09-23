"""Миграция 0127: таблицы напоминаний и встроенное «привяжите второй способ входа»."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database.models import Base, UserReminder, UserReminderState, User
from tests.fixtures.sqlite_memory import memory_session


MIGRATION = Path('migrations/alembic/versions/0127_user_reminders.py')


def _migration():
    spec = importlib.util.spec_from_file_location('m0127', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    module = _migration()
    assert module.revision == '0127'
    assert module.down_revision == '0126'


def test_builtin_is_shipped_disabled_with_all_languages():
    row = _migration().BUILTIN_LINK_AUTH
    assert row['builtin_key'] == 'link_auth_method'
    assert row['is_active'] is False
    assert row['channels'] == 'both'
    assert row['category'] == 'service'
    assert row['conditions'] == {'auth': 'single_method', 'registered_days_min': 3}
    assert (row['repeat_every_days'], row['max_sends']) == (14, 3)
    assert (row['button_kind'], row['button_target']) == ('cabinet', '/profile/accounts')
    assert set(row['texts']) == {'ru', 'en', 'ua', 'zh', 'fa'}
    for text in row['texts'].values():
        assert 1 <= len(text['title']) <= 80
        assert 1 <= len(text['body']) <= 1000
        assert 1 <= len(text['button']) <= 40


@pytest.mark.asyncio
async def test_models_roundtrip(monkeypatch):
    async with memory_session(monkeypatch, list(Base.metadata.sorted_tables)) as db:
        db.add(User(id=1, telegram_id=10, first_name='U', language='ru', status='active', balance_kopeks=0))
        reminder = UserReminder(
            name='r',
            channels='bot',
            category='service',
            conditions={},
            repeat_every_days=7,
            max_sends=1,
            texts={'ru': {'title': 't', 'body': 'b'}},
            button_kind='none',
        )
        db.add(reminder)
        await db.flush()
        db.add(UserReminderState(reminder_id=reminder.id, user_id=1))
        await db.commit()

        state = (await db.execute(select(UserReminderState))).scalar_one()
        assert state.sends_count == 0
        assert reminder.is_active is False
        assert reminder.builtin_key is None
