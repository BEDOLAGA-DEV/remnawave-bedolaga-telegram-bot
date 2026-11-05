"""CRUD состояний премиум-лимита: жизненный цикл периода и защита от гонки."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.crud.premium_traffic import (
    add_extra_bytes,
    delete_states_for_squads,
    delete_states_for_subscription,
    get_limited_squad_uuids,
    get_or_create_state,
    get_state,
    get_states_for_squad,
    get_states_for_subscription,
    record_usage,
    start_new_period,
)
from app.database.models import SubscriptionPremiumTraffic
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


# Таблица подписок не создаётся: sqlite не проверяет внешние ключи, если их не
# включить явно, а тесты здесь про состояния, а не про целостность связей.
TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER_SQUAD = '82a12389-14d6-40c6-b320-4674f6bbb344'

FIVE_GB = 5 * BYTES_IN_GB
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


async def _new_state(db, subscription_id=1, squad_uuid=SQUAD, limit_bytes=FIVE_GB, period_start_at=NOW):
    return await get_or_create_state(
        db,
        subscription_id,
        squad_uuid,
        limit_bytes=limit_bytes,
        period_start_at=period_start_at,
    )


async def test_state_is_created_once_and_read_back(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        created = await _new_state(db)
        await db.commit()

        assert created.limit_bytes == FIVE_GB
        assert created.used_bytes == 0
        assert created.is_limited is False

        again = await _new_state(db)
        assert again.id == created.id

        found = await get_state(db, 1, SQUAD)
        assert found is not None and found.id == created.id


async def test_lost_insert_race_returns_the_existing_row(monkeypatch):
    """Воркер и покупка трафика могут дойти до вставки одновременно.

    Имитируем проигранную гонку: первая проверка «есть ли строка» не видит
    чужую вставку, INSERT падает на уникальном ключе — и состояние должно
    вернуться перечитанным, а не всплыть исключением.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        winner = await _new_state(db)
        await db.commit()

        real_get_state = get_state
        calls = {'n': 0}

        async def _blind_once(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return None  # гонка: чужую строку ещё не видим
            return await real_get_state(*args, **kwargs)

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _blind_once)

        recovered = await _new_state(db)

        assert recovered.id == winner.id
        assert calls['n'] == 2  # первая проверка и перечитывание после конфликта
        monkeypatch.undo()
        assert len(await get_states_for_subscription(db, 1)) == 1


async def test_foreign_integrity_error_is_not_swallowed(monkeypatch):
    """Если упал не наш уникальный ключ — ошибку прятать нельзя."""
    async with memory_session(monkeypatch, TABLES) as db:

        async def _always_blind(*_args, **_kwargs):
            return None

        monkeypatch.setattr('app.database.crud.premium_traffic.get_state', _always_blind)
        await _new_state(db)
        await db.commit()

        with pytest.raises(IntegrityError):
            await _new_state(db)


async def test_usage_never_goes_down_inside_a_period(monkeypatch):
    """Просадка выборки не должна вернуть доступ к исчерпанному скваду."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)

        record_usage(state, 4 * BYTES_IN_GB)
        assert state.used_bytes == 4 * BYTES_IN_GB

        record_usage(state, BYTES_IN_GB)
        assert state.used_bytes == 4 * BYTES_IN_GB

        record_usage(state, -1)
        assert state.used_bytes == 4 * BYTES_IN_GB


async def test_usage_records_check_time(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)

        record_usage(state, BYTES_IN_GB, checked_at=NOW)

        assert state.last_checked_at == NOW


async def test_exhaustion_and_topup_return_the_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)

        record_usage(state, FIVE_GB)
        assert state.is_exhausted is True

        state.is_limited = True
        state.notified_80 = True

        add_extra_bytes(state, 2 * BYTES_IN_GB)

        assert state.extra_bytes == 2 * BYTES_IN_GB
        assert state.is_exhausted is False
        assert state.is_limited is False
        # Порог 80 % теперь считается от 7 ГБ — предупредить нужно заново.
        assert state.notified_80 is False


async def test_topup_smaller_than_overspend_keeps_the_squad_limited(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        record_usage(state, 8 * BYTES_IN_GB)
        state.is_limited = True

        add_extra_bytes(state, BYTES_IN_GB)

        assert state.is_exhausted is True
        assert state.is_limited is True


async def test_non_positive_topup_changes_nothing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.is_limited = True

        add_extra_bytes(state, 0)
        add_extra_bytes(state, -BYTES_IN_GB)

        assert state.extra_bytes == 0
        assert state.is_limited is True


async def test_new_period_resets_everything_and_takes_fresh_limit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        record_usage(state, FIVE_GB)
        add_extra_bytes(state, BYTES_IN_GB)
        state.is_limited = True
        state.notified_80 = True

        next_period = NOW + timedelta(days=30)
        start_new_period(state, period_start_at=next_period, limit_bytes=10 * BYTES_IN_GB)

        assert state.period_start_at == next_period
        # Лимит перечитан из тарифа: за прошедший период его могли поменять.
        assert state.limit_bytes == 10 * BYTES_IN_GB
        assert state.used_bytes == 0
        assert state.extra_bytes == 0
        assert state.notified_80 is False
        assert state.is_limited is False


async def test_new_period_clears_the_first_day_correction(monkeypatch):
    """Период новый — поправку на его первые сутки надо снять заново."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.baseline_bytes = 3 * BYTES_IN_GB

        start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=FIVE_GB)

        assert state.baseline_bytes is None


async def test_new_period_keeps_ack_when_not_given(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await _new_state(db)
        state.panel_reset_ack_at = NOW

        start_new_period(state, period_start_at=NOW + timedelta(days=30), limit_bytes=FIVE_GB)

        assert state.panel_reset_ack_at == NOW


async def test_limited_squads_are_listed_for_the_subscription(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        limited = await _new_state(db, squad_uuid=SQUAD)
        await _new_state(db, squad_uuid=OTHER_SQUAD)
        limited.is_limited = True
        await db.commit()

        assert await get_limited_squad_uuids(db, 1) == {SQUAD}


async def test_limited_squads_of_other_subscriptions_do_not_leak(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        theirs = await _new_state(db, subscription_id=2)
        theirs.is_limited = True
        await db.commit()

        assert await get_limited_squad_uuids(db, 1) == set()


async def test_states_are_collected_per_squad_for_the_worker(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, subscription_id=1, squad_uuid=SQUAD)
        await _new_state(db, subscription_id=2, squad_uuid=SQUAD)
        await _new_state(db, subscription_id=3, squad_uuid=OTHER_SQUAD)
        await db.commit()

        by_squad = await get_states_for_squad(db, SQUAD)

        assert {s.subscription_id for s in by_squad} == {1, 2}


async def test_states_are_deleted_for_a_subscription(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, squad_uuid=SQUAD)
        await _new_state(db, squad_uuid=OTHER_SQUAD)
        await db.commit()

        deleted = await delete_states_for_subscription(db, 1)
        await db.commit()

        assert deleted == 2
        assert await get_states_for_subscription(db, 1) == []


async def test_only_named_squads_are_deleted(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db, squad_uuid=SQUAD)
        await _new_state(db, squad_uuid=OTHER_SQUAD)
        await db.commit()

        deleted = await delete_states_for_squads(db, 1, {SQUAD})
        await db.commit()

        assert deleted == 1
        remaining = await get_states_for_subscription(db, 1)
        assert [s.squad_uuid for s in remaining] == [OTHER_SQUAD]


async def test_deleting_nothing_is_a_no_op(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _new_state(db)
        await db.commit()

        assert await delete_states_for_squads(db, 1, set()) == 0
        assert len(await get_states_for_subscription(db, 1)) == 1
