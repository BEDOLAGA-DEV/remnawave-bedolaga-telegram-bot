"""Данные премиум-трафика для карточки расхода в мини-аппе."""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.cabinet.routes.subscription_modules.helpers import build_premium_traffic_info
from app.database.crud.premium_traffic import get_or_create_state
from app.database.models import ServerSquad, SubscriptionPremiumTraffic
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


# Имена премиум-строк подставляются из справочника серверов.
TABLES = (SubscriptionPremiumTraffic.__table__, ServerSquad.__table__)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
PLAIN_SQUAD = '82a12389-14d6-40c6-b320-4674f6bbb344'
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _subscription(limits, connected=(SQUAD,), subscription_id=1):
    return SimpleNamespace(
        id=subscription_id,
        connected_squads=list(connected),
        tariff=SimpleNamespace(server_traffic_limits=limits),
    )


async def test_tariff_without_premium_squads_returns_nothing(monkeypatch):
    """Пустой список — мини-апп не рисует блок вовсе."""
    async with memory_session(monkeypatch, TABLES) as db:
        assert await build_premium_traffic_info(db, _subscription({})) == []


async def test_subscription_without_tariff_is_safe(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        bare = SimpleNamespace(id=1, connected_squads=[SQUAD], tariff=None)

        assert await build_premium_traffic_info(db, bare) == []


async def test_limit_is_shown_before_the_worker_ever_ran(monkeypatch):
    """Пользователь должен видеть лимит, не дожидаясь первого прохода воркера."""
    async with memory_session(monkeypatch, TABLES) as db:
        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert len(info) == 1
        assert info[0].squad_uuid == SQUAD
        assert info[0].limit_gb == 5
        assert info[0].used_gb == 0
        assert info[0].used_percent == 0
        assert info[0].is_limited is False
        assert info[0].period_start_at is None


async def test_usage_is_taken_from_the_state(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        state.used_bytes = 2 * BYTES_IN_GB
        await db.commit()

        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert info[0].used_gb == 2
        assert info[0].used_percent == 40.0
        assert info[0].period_start_at == NOW


async def test_topped_up_traffic_is_shown_separately(monkeypatch):
    """Видно, что пользователь докупал, а не просто «лимит стал больше»."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        state.extra_bytes = 3 * BYTES_IN_GB
        state.used_bytes = 4 * BYTES_IN_GB
        await db.commit()

        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert info[0].limit_gb == 5
        assert info[0].extra_gb == 3
        # Процент считается от полного лимита с докупкой: 4 из 8.
        assert info[0].used_percent == 50.0


async def test_percent_never_exceeds_hundred(monkeypatch):
    """Перерасход между проходами воркера не должен ломать шкалу в интерфейсе."""
    async with memory_session(monkeypatch, TABLES) as db:
        state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        state.used_bytes = 9 * BYTES_IN_GB
        await db.commit()

        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert info[0].used_percent == 100.0


async def test_limited_squad_is_flagged(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        state.is_limited = True
        await db.commit()

        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert info[0].is_limited is True


async def _add_squad(db, squad_uuid=SQUAD, display_name='LTE'):
    db.add(ServerSquad(squad_uuid=squad_uuid, display_name=display_name, is_available=True))
    await db.commit()


async def test_name_falls_back_to_the_server_name(monkeypatch):
    """Без названия строки премиума в интерфейсе неразличимы."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _add_squad(db, display_name='Мобильный резерв')

        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert info[0].name == 'Мобильный резерв'


async def test_custom_name_wins_over_the_server_name(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _add_squad(db, display_name='LTE')

        info = await build_premium_traffic_info(
            db, _subscription({SQUAD: {'traffic_limit_gb': 5, 'name': 'Мобильный резерв'}})
        )

        assert info[0].name == 'Мобильный резерв'


async def test_unknown_server_leaves_the_name_empty(monkeypatch):
    """Сервер удалили из справочника — интерфейс подставит общий заголовок."""
    async with memory_session(monkeypatch, TABLES) as db:
        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert info[0].name is None


async def test_squad_outside_the_subscription_is_not_shown(monkeypatch):
    """Лимит в тарифе задан, но подписка на этот сквад права не даёт."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = _subscription({SQUAD: {'traffic_limit_gb': 5}}, connected=(PLAIN_SQUAD,))

        assert await build_premium_traffic_info(db, subscription) == []


async def test_topup_availability_comes_from_the_tariff(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        with_topup = {SQUAD: {'traffic_limit_gb': 5, 'topup_enabled': True, 'topup_packages': {'1': 500}}}

        info = await build_premium_traffic_info(db, _subscription(with_topup))
        assert info[0].topup_available is True

        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))
        assert info[0].topup_available is False


async def test_states_of_other_subscriptions_do_not_leak(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        theirs = await get_or_create_state(db, 2, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
        theirs.used_bytes = 4 * BYTES_IN_GB
        await db.commit()

        info = await build_premium_traffic_info(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert info[0].used_gb == 0
