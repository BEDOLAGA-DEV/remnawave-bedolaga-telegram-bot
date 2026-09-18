"""Докупка премиум-трафика: правила доступности, цены и потолка."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.database.crud.premium_traffic import get_or_create_state
from app.database.models import SubscriptionPremiumTraffic
from app.services.premium_traffic_purchase import (
    PremiumTopupError,
    apply_premium_topup,
    get_premium_topup_options,
    quote_premium_topup,
)
from app.utils.premium_traffic import BYTES_IN_GB
from tests.fixtures.sqlite_memory import memory_session


TABLES = (SubscriptionPremiumTraffic.__table__,)

SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER = '82a12389-14d6-40c6-b320-4674f6bbb344'
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

WITH_TOPUP = {
    'traffic_limit_gb': 5,
    'topup_enabled': True,
    'topup_packages': {'1': 500, '5': 2000},
    'max_topup_gb': 10,
}


def _subscription(limits=None, connected=(SQUAD,), subscription_id=1):
    return SimpleNamespace(
        id=subscription_id,
        connected_squads=list(connected),
        tariff=SimpleNamespace(server_traffic_limits=limits if limits is not None else {SQUAD: WITH_TOPUP}),
    )


class TestOptions:
    def test_squad_with_topup_is_offered(self):
        options = get_premium_topup_options(_subscription())

        assert list(options) == [SQUAD]
        assert options[SQUAD].available_packages() == [(1, 500), (5, 2000)]

    def test_topup_disabled_squad_is_not_offered(self):
        options = get_premium_topup_options(_subscription({SQUAD: {'traffic_limit_gb': 5}}))

        assert options == {}

    def test_squad_outside_the_subscription_is_not_offered(self):
        """Платить за трафик по серверу, которого нет в подписке, нельзя."""
        options = get_premium_topup_options(_subscription(connected=(OTHER,)))

        assert options == {}

    def test_tariff_without_premium_offers_nothing(self):
        assert get_premium_topup_options(_subscription({})) == {}


class TestQuote:
    async def test_known_package_is_priced(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            quote = await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert quote.base_price_kopeks == 2000
            assert quote.bytes == 5 * BYTES_IN_GB

    async def test_unknown_package_is_rejected(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription(), SQUAD, 3)

            assert error.value.code == 'package_not_found'

    async def test_squad_without_topup_is_rejected(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription({SQUAD: {'traffic_limit_gb': 5}}), SQUAD, 5)

            assert error.value.code == 'topup_unavailable'

    async def test_ceiling_counts_what_was_already_bought(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 8 * BYTES_IN_GB
            await db.commit()

            with pytest.raises(PremiumTopupError) as error:
                await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert error.value.code == 'topup_limit_reached'
            assert '10' in error.value.message

    async def test_purchase_up_to_the_ceiling_is_allowed(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 5 * BYTES_IN_GB
            await db.commit()

            quote = await quote_premium_topup(db, _subscription(), SQUAD, 5)

            assert quote.gb == 5

    async def test_zero_ceiling_means_no_limit(self, monkeypatch):
        limits = {SQUAD: {**WITH_TOPUP, 'max_topup_gb': 0}}
        async with memory_session(monkeypatch, TABLES) as db:
            state = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            state.extra_bytes = 500 * BYTES_IN_GB
            await db.commit()

            quote = await quote_premium_topup(db, _subscription(limits), SQUAD, 5)

            assert quote.gb == 5


class TestApply:
    async def test_bought_volume_is_credited(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 5)

            state, restored = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert state.extra_bytes == 5 * BYTES_IN_GB
            assert restored is False

    async def test_state_is_created_when_worker_never_ran(self, monkeypatch):
        """Покупка не должна ждать первого прохода воркера."""
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 1)

            state, _ = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert state.id is not None
            assert state.limit_bytes == 5 * BYTES_IN_GB

    async def test_limited_squad_is_reported_as_restored(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            existing = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            existing.used_bytes = 5 * BYTES_IN_GB
            existing.is_limited = True
            await db.commit()

            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 5)
            state, restored = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert restored is True
            assert state.is_limited is False

    async def test_topup_smaller_than_overspend_does_not_restore(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            existing = await get_or_create_state(db, 1, SQUAD, limit_bytes=5 * BYTES_IN_GB, period_start_at=NOW)
            existing.used_bytes = 20 * BYTES_IN_GB
            existing.is_limited = True
            await db.commit()

            subscription = _subscription()
            quote = await quote_premium_topup(db, subscription, SQUAD, 1)
            state, restored = await apply_premium_topup(db, subscription, quote, period_start_at=NOW)

            assert restored is False
            assert state.is_limited is True

    async def test_second_purchase_adds_up(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            subscription = _subscription()

            first = await quote_premium_topup(db, subscription, SQUAD, 1)
            await apply_premium_topup(db, subscription, first, period_start_at=NOW)
            second = await quote_premium_topup(db, subscription, SQUAD, 5)
            state, _ = await apply_premium_topup(db, subscription, second, period_start_at=NOW)

            assert state.extra_bytes == 6 * BYTES_IN_GB
