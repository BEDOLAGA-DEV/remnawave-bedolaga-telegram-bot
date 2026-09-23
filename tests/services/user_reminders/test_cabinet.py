from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.database.models import Base, User, UserReminder
from app.services.user_reminders import cabinet as cabinet_service
from app.services.user_reminders.cabinet import active_cards_for_user, dismiss_reminder
from tests.fixtures.sqlite_memory import memory_session


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
TABLES = list(Base.metadata.sorted_tables)
TEXTS = {'ru': {'title': 'Привет', 'body': 'Текст', 'button': 'Открыть'}, 'en': {'title': 'Hi', 'body': 'Body'}}
BROKEN_TEXTS = {'en': {'title': 't', 'body': 'b'}}


def _reminder(rid, **kw):
    base = dict(
        id=rid,
        name=f'r{rid}',
        channels='cabinet',
        category='service',
        conditions={},
        repeat_every_days=7,
        max_sends=1,
        texts=TEXTS,
        button_kind='cabinet',
        button_target='/profile/accounts',
        is_active=True,
    )
    base.update(kw)
    return UserReminder(**base)


async def _seed(db, reminders):
    db.add(
        User(
            id=1,
            telegram_id=10,
            first_name='U',
            language='ru',
            status='active',
            balance_kopeks=0,
            created_at=NOW - timedelta(days=30),
        )
    )
    db.add_all(reminders)
    await db.commit()
    return await db.get(User, 1)


@pytest.mark.asyncio
async def test_cards_follow_conditions_channels_and_order(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(
            db,
            [
                _reminder(1),
                _reminder(2, builtin_key='k'),
                _reminder(3, channels='bot'),
                _reminder(4, is_active=False),
                _reminder(5, conditions={'auth': 'email_only'}),
            ],
        )
        cards = await active_cards_for_user(db, user, lang='en', now=NOW)

    assert [card['id'] for card in cards] == [2, 1]
    assert cards[0] == {
        'id': 2,
        'title': 'Hi',
        'body': 'Body',
        'button': {'kind': 'cabinet', 'target': '/profile/accounts', 'text': 'Открыть'},
    }


@pytest.mark.asyncio
async def test_dismissed_card_is_gone_and_dismiss_is_idempotent(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, [_reminder(1)])
        assert await dismiss_reminder(db, user, 1, now=NOW) is True
        assert await dismiss_reminder(db, user, 1, now=NOW + timedelta(hours=1)) is True
        assert await active_cards_for_user(db, user, lang='ru', now=NOW) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reminder_kw', [{'is_active': False}, {'channels': 'bot'}])
async def test_dismiss_unknown_or_not_for_cabinet_is_false(monkeypatch, reminder_kw):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, [_reminder(1, **reminder_kw)])
        assert await dismiss_reminder(db, user, 1, now=NOW) is False
        assert await dismiss_reminder(db, user, 999, now=NOW) is False


@pytest.mark.asyncio
async def test_broken_conditions_do_not_break_the_page(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, [_reminder(1, conditions={'auth': 'garbage'}), _reminder(2)])
        cards = await active_cards_for_user(db, user, lang='ru', now=NOW)
    assert [card['id'] for card in cards] == [2]


@pytest.mark.asyncio
async def test_broken_texts_do_not_break_the_page(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, [_reminder(1, texts=BROKEN_TEXTS), _reminder(2)])
        cards = await active_cards_for_user(db, user, lang='ru', now=NOW)
    assert [card['id'] for card in cards] == [2]


@pytest.mark.asyncio
async def test_dismiss_returns_false_when_state_cannot_be_created(monkeypatch):
    """get_or_create_state вернёт None при FK-гонке (напоминание/юзер удалены между
    проверкой и вставкой) — dismiss_reminder не должен падать AttributeError, роут
    превращает False в 404.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, [_reminder(1)])
        monkeypatch.setattr(cabinet_service, 'get_or_create_state', AsyncMock(return_value=None))

        assert await dismiss_reminder(db, user, 1, now=NOW) is False
