"""Список grace-сессий и сборщик их состояния — на реальных запросах к БД.

Счётчики из этого сборщика печатает аварийный CLI и показывает раздел кабинета:
руководство по откату сверяет числа «до» и «после» ``restore-all``, поэтому набор
ключей здесь — контракт, а не деталь реализации.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cabinet.routes import admin_grace_access as route
from app.database.models import GraceAccessSessionModel, Subscription, User
from app.services.grace_access_runtime import collect_grace_status
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, Subscription.__table__, GraceAccessSessionModel.__table__)

ADMIN = SimpleNamespace(id=1, telegram_id=1)
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


async def _seed_user(db, *, user_id: int, telegram_id: int) -> None:
    db.add(
        User(
            id=user_id,
            telegram_id=telegram_id,
            username=f'user{user_id}',
            first_name='Имя',
            last_name='Фамилия',
            language='ru',
        )
    )
    db.add(
        Subscription(
            id=user_id,
            user_id=user_id,
            status='active',
            start_date=NOW - timedelta(days=30),
            end_date=NOW,
            # Уникальное непустое значение: server_default='' с UNIQUE роняет вторую строку.
            remnawave_short_id=f'short{user_id}',
        )
    )
    await db.flush()


async def _seed_session(
    db,
    *,
    session_id: str,
    subscription_id: int,
    state: str,
    minutes_ago: int,
    last_error: str | None = None,
    completion_reason: str | None = None,
) -> None:
    db.add(
        GraceAccessSessionModel(
            id=session_id,
            subscription_id=subscription_id,
            remnawave_id=1000 + subscription_id,
            reason='expired',
            incident_key=f'incident-{session_id}',
            state=state,
            snapshot_version=3,
            version=1,
            billing_before={},
            panel_before={},
            overlay={},
            started_at=NOW - timedelta(days=1),
            grace_until=NOW + timedelta(days=2),
            updated_at=NOW - timedelta(minutes=minutes_ago),
            completion_reason=completion_reason,
            completed_at=NOW if completion_reason else None,
            last_error=last_error,
        )
    )
    await db.flush()


class TestStatusSnapshot:
    @pytest.mark.asyncio
    async def test_keys_match_the_rollback_runbook(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            snapshot = await collect_grace_status(db)

        assert set(snapshot) == {
            'open',
            'open_errors',
            'completed_errors',
            'with_errors',
            'states',
            'recent_errors',
        }
        assert snapshot['open'] == 0
        assert snapshot['recent_errors'] == []

    @pytest.mark.asyncio
    async def test_open_counts_every_unfinished_state(self, monkeypatch):
        """Отдельные счётчики по состояниям не заменяют «сколько всего висит»."""
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed_user(db, user_id=1, telegram_id=100)
            await _seed_user(db, user_id=2, telegram_id=200)
            await _seed_user(db, user_id=3, telegram_id=300)
            await _seed_user(db, user_id=4, telegram_id=400)
            await _seed_session(db, session_id='a', subscription_id=1, state='pending', minutes_ago=1)
            await _seed_session(db, session_id='b', subscription_id=2, state='active', minutes_ago=2)
            await _seed_session(db, session_id='c', subscription_id=3, state='restoring', minutes_ago=3)
            await _seed_session(
                db,
                session_id='d',
                subscription_id=4,
                state='completed',
                minutes_ago=4,
                completion_reason='paid',
            )

            snapshot = await collect_grace_status(db)

        assert snapshot['open'] == 3
        assert snapshot['states'] == {'active': 1, 'completed': 1, 'pending': 1, 'restoring': 1}

    @pytest.mark.asyncio
    async def test_open_and_completed_errors_are_counted_apart(self, monkeypatch):
        """Ошибка в открытой сессии требует вмешательства, в завершённой — разбора."""
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed_user(db, user_id=1, telegram_id=100)
            await _seed_user(db, user_id=2, telegram_id=200)
            await _seed_session(
                db, session_id='a', subscription_id=1, state='active', minutes_ago=1, last_error='живая'
            )
            await _seed_session(
                db,
                session_id='b',
                subscription_id=2,
                state='completed',
                minutes_ago=2,
                completion_reason='conflict',
                last_error='терминальная',
            )

            snapshot = await collect_grace_status(db)

        assert snapshot['open_errors'] == 1
        assert snapshot['completed_errors'] == 1
        assert snapshot['with_errors'] == 2

    @pytest.mark.asyncio
    async def test_recent_errors_are_newest_first_and_capped(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            for index in range(1, 4):
                await _seed_user(db, user_id=index, telegram_id=100 * index)
                await _seed_session(
                    db,
                    session_id=f's{index}',
                    subscription_id=index,
                    state='active',
                    minutes_ago=index,
                    last_error=f'ошибка {index}',
                )

            snapshot = await collect_grace_status(db, error_limit=2)

        assert [row['id'] for row in snapshot['recent_errors']] == ['s1', 's2']
        assert snapshot['recent_errors'][0]['last_error'] == 'ошибка 1'


class TestSessionsList:
    @pytest.mark.asyncio
    async def test_open_filter_excludes_finished_sessions(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed_user(db, user_id=1, telegram_id=100)
            await _seed_user(db, user_id=2, telegram_id=200)
            await _seed_session(db, session_id='open', subscription_id=1, state='active', minutes_ago=1)
            await _seed_session(
                db,
                session_id='done',
                subscription_id=2,
                state='completed',
                minutes_ago=2,
                completion_reason='paid',
            )

            page = await route.list_grace_sessions(admin=ADMIN, db=db, state='open')

        assert [item.id for item in page.items] == ['open']
        assert page.total == 1

    @pytest.mark.asyncio
    async def test_errors_filter_ignores_state(self, monkeypatch):
        """Терминальный конфликт закрыт, но именно его и ищут после аварии."""
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed_user(db, user_id=1, telegram_id=100)
            await _seed_user(db, user_id=2, telegram_id=200)
            await _seed_session(db, session_id='clean', subscription_id=1, state='active', minutes_ago=1)
            await _seed_session(
                db,
                session_id='broken',
                subscription_id=2,
                state='completed',
                minutes_ago=2,
                completion_reason='conflict',
                last_error='panel rejected',
            )

            page = await route.list_grace_sessions(admin=ADMIN, db=db, state='errors')

        assert [item.id for item in page.items] == ['broken']

    @pytest.mark.asyncio
    async def test_owner_is_resolved_for_each_session(self, monkeypatch):
        """Без владельца строка бесполезна: id подписки ничего не говорит о том, кому писать."""
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed_user(db, user_id=7, telegram_id=777)
            await _seed_session(db, session_id='a', subscription_id=7, state='active', minutes_ago=1)

            page = await route.list_grace_sessions(admin=ADMIN, db=db)

        assert page.items[0].user is not None
        assert page.items[0].user.telegram_id == 777
        assert page.items[0].user.full_name == 'Имя Фамилия'

    @pytest.mark.asyncio
    async def test_pagination_walks_the_whole_list(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            for index in range(1, 4):
                await _seed_user(db, user_id=index, telegram_id=100 * index)
                await _seed_session(
                    db, session_id=f's{index}', subscription_id=index, state='active', minutes_ago=index
                )

            first = await route.list_grace_sessions(admin=ADMIN, db=db, page=1, limit=2)
            second = await route.list_grace_sessions(admin=ADMIN, db=db, page=2, limit=2)

        assert [item.id for item in first.items] == ['s1', 's2']
        assert [item.id for item in second.items] == ['s3']
        assert first.total == second.total == 3
