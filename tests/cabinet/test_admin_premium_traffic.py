"""Админский сброс премиум-трафика и разведение областей сброса."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.cabinet.routes.admin_premium_traffic import _reset_premium, _reset_regular
from app.database.crud.premium_traffic import get_or_create_state, get_states_for_subscription
from app.database.models import SubscriptionPremiumTraffic
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER = '82a12389-14d6-40c6-b320-4674f6bbb344'
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
PANEL_RESET_AT = datetime(2026, 9, 6, 11, 55, tzinfo=UTC)


def _subscription(limits=None, subscription_id=1):
    return SimpleNamespace(
        id=subscription_id,
        remnawave_id=42,
        user=SimpleNamespace(remnawave_id=42),
        connected_squads=[SQUAD, OTHER],
        tariff=SimpleNamespace(
            server_traffic_limits=limits
            if limits is not None
            else {SQUAD: {'traffic_limit_gb': 5}, OTHER: {'traffic_limit_gb': 10}}
        ),
    )


class _FakeApi:
    def __init__(self):
        self.reset_calls: list[int] = []

    async def reset_user_traffic(self, user_id):
        self.reset_calls.append(user_id)

    async def get_user_by_id(self, user_id):
        return SimpleNamespace(last_traffic_reset_at=PANEL_RESET_AT)


class _FakeService:
    is_configured = True

    def __init__(self, api):
        self._api = api

    def get_api_client(self):
        api = self._api

        class _Ctx:
            async def __aenter__(self):
                return api

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


async def _spent_state(db, squad_uuid=SQUAD, subscription_id=1):
    state = await get_or_create_state(
        db, subscription_id, squad_uuid, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW - timedelta(days=3)
    )
    state.used_bytes = 5 * BYTES_IN_GB
    state.extra_bytes = 2 * BYTES_IN_GB
    state.is_limited = True
    state.notified_80 = True
    await db.commit()
    return state


class TestPremiumReset:
    async def test_period_starts_over_and_squad_returns(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            state = await _spent_state(db)

            reset = await _reset_premium(db, _subscription(), SQUAD, NOW)

            assert reset == [SQUAD]
            assert state.period_start_at == NOW
            assert state.used_bytes == 0
            assert state.extra_bytes == 0
            assert state.notified_80 is False
            assert state.is_limited is False

    async def test_without_a_squad_all_premium_squads_are_reset(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            await _spent_state(db, SQUAD)
            await _spent_state(db, OTHER)

            reset = await _reset_premium(db, _subscription(), None, NOW)

            assert set(reset) == {SQUAD, OTHER}
            for state in await get_states_for_subscription(db, 1):
                assert state.is_limited is False

    async def test_squad_outside_the_tariff_is_ignored(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            assert await _reset_premium(db, _subscription(), 'unknown-squad', NOW) == []

    async def test_tariff_without_premium_resets_nothing(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            assert await _reset_premium(db, _subscription({}), None, NOW) == []

    async def test_state_is_created_if_the_worker_never_ran(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            reset = await _reset_premium(db, _subscription(), SQUAD, NOW)
            await db.commit()

            assert reset == [SQUAD]
            states = await get_states_for_subscription(db, 1)
            assert len(states) == 1
            assert states[0].limit_bytes == 5 * BYTES_IN_GB


class TestRegularReset:
    async def test_panel_is_reset_and_premium_is_left_alone(self, monkeypatch):
        """Главное требование: области сброса не пересекаются."""
        api = _FakeApi()
        monkeypatch.setattr('app.cabinet.routes.admin_premium_traffic.RemnaWaveService', lambda: _FakeService(api))
        async with memory_session(monkeypatch, TABLES) as db:
            state = await _spent_state(db)
            period_before = state.period_start_at

            assert await _reset_regular(db, _subscription(), 'regular', NOW) is True

            assert api.reset_calls == [42]
            assert state.used_bytes == 5 * BYTES_IN_GB
            assert state.is_limited is True
            assert state.period_start_at == period_before

    async def test_panel_reset_is_acknowledged_so_the_worker_ignores_it(self, monkeypatch):
        """Иначе воркер примет его за досрочный сброс и обнулит премиум следом."""
        api = _FakeApi()
        monkeypatch.setattr('app.cabinet.routes.admin_premium_traffic.RemnaWaveService', lambda: _FakeService(api))
        async with memory_session(monkeypatch, TABLES) as db:
            state = await _spent_state(db)

            await _reset_regular(db, _subscription(), 'regular', NOW)

            assert state.panel_reset_ack_at == PANEL_RESET_AT

    async def test_both_scope_does_not_acknowledge(self, monkeypatch):
        """При 'both' премиум сбрасывается явно — отмечать нечего."""
        api = _FakeApi()
        monkeypatch.setattr('app.cabinet.routes.admin_premium_traffic.RemnaWaveService', lambda: _FakeService(api))
        async with memory_session(monkeypatch, TABLES) as db:
            state = await _spent_state(db)

            await _reset_regular(db, _subscription(), 'both', NOW)

            assert state.panel_reset_ack_at is None

    async def test_subscription_without_panel_user_is_rejected(self, monkeypatch):
        import pytest
        from fastapi import HTTPException

        api = _FakeApi()
        monkeypatch.setattr('app.cabinet.routes.admin_premium_traffic.RemnaWaveService', lambda: _FakeService(api))
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()
            subscription.remnawave_id = None
            subscription.user = SimpleNamespace(remnawave_id=None)

            with pytest.raises(HTTPException) as error:
                await _reset_regular(db, subscription, 'regular', NOW)

            assert error.value.status_code == 409
            assert api.reset_calls == []
