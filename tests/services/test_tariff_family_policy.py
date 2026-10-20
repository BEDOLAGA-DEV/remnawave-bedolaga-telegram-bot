"""Регрессии поиска конфликтов семейства; не заменяют конкурентный PG-тест."""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import PromoGroup, Subscription, SubscriptionStatus, Tariff, tariff_promo_groups
from app.services.tariff_family_policy import find_live_entitlement_family_conflict
from tests.fixtures.sqlite_memory import memory_session


TABLES = (PromoGroup.__table__, Tariff.__table__, tariff_promo_groups, Subscription.__table__)


def _subscription(user_id: int, tariff_id: int, status: SubscriptionStatus) -> Subscription:
    return Subscription(
        user_id=user_id,
        tariff_id=tariff_id,
        remnawave_short_id=f'family-{user_id}-{tariff_id}',
        status=status.value,
        is_trial=status == SubscriptionStatus.TRIAL,
        start_date=datetime.now(UTC),
        end_date=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_other_main_tariff_is_conflict_but_independent_addon_is_not(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        start = Tariff(name='Start', entitlement_family='main')
        standard = Tariff(name='Standard', entitlement_family='main')
        addon = Tariff(name='CDN', entitlement_family='metered_bypass')
        db.add_all([start, standard, addon])
        await db.flush()

        main = _subscription(47, start.id, SubscriptionStatus.ACTIVE)
        addon_sub = _subscription(47, addon.id, SubscriptionStatus.LIMITED)
        db.add_all([main, addon_sub])
        await db.flush()

        conflict = await find_live_entitlement_family_conflict(db, user_id=47, tariff_id=standard.id)
        assert conflict is main
        assert (
            await find_live_entitlement_family_conflict(
                db, user_id=47, tariff_id=addon.id, current_subscription_id=addon_sub.id
            )
            is None
        )
        assert await find_live_entitlement_family_conflict(db, user_id=48, tariff_id=standard.id) is None


@pytest.mark.asyncio
async def test_trial_and_limited_count_as_live_but_expired_and_disabled_do_not(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        target = Tariff(name='Standard', entitlement_family='main')
        old = Tariff(name='Start', entitlement_family='main')
        db.add_all([target, old])
        await db.flush()

        record = _subscription(49, old.id, SubscriptionStatus.EXPIRED)
        db.add(record)
        await db.flush()

        assert await find_live_entitlement_family_conflict(db, user_id=49, tariff_id=target.id) is None

        for state in (SubscriptionStatus.TRIAL, SubscriptionStatus.LIMITED, SubscriptionStatus.ACTIVE):
            record.status = state.value
            await db.flush()
            assert await find_live_entitlement_family_conflict(db, user_id=49, tariff_id=target.id) is record

        record.status = SubscriptionStatus.DISABLED.value
        await db.flush()
        assert await find_live_entitlement_family_conflict(db, user_id=49, tariff_id=target.id) is None


@pytest.mark.asyncio
async def test_legacy_null_family_unrestricted_and_missing_tariff_is_not_null(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        old = Tariff(name='Legacy')
        db.add(old)
        await db.flush()
        db.add(_subscription(51, old.id, SubscriptionStatus.ACTIVE))
        await db.flush()

        assert await find_live_entitlement_family_conflict(db, user_id=51, tariff_id=old.id) is None
        with pytest.raises(ValueError, match='не найден'):
            await find_live_entitlement_family_conflict(db, user_id=51, tariff_id=999999)


@pytest.mark.asyncio
async def test_current_subscription_not_conflict_when_switching_or_renewing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        basic = Tariff(name='Basic', entitlement_family='main')
        premium = Tariff(name='Premium', entitlement_family='main')
        db.add_all([basic, premium])
        await db.flush()
        current = _subscription(53, basic.id, SubscriptionStatus.ACTIVE)
        db.add(current)
        await db.flush()

        assert (
            await find_live_entitlement_family_conflict(
                db, user_id=53, tariff_id=premium.id, current_subscription_id=current.id
            )
            is None
        )
        assert await find_live_entitlement_family_conflict(db, user_id=53, tariff_id=basic.id) is current
