"""Регрессии поиска конфликтов семейства; не заменяют конкурентный PG-тест."""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.subscription import (
    deactivate_user_trial_subscriptions,
    get_alive_trial_subscription,
    resolve_trial_conversion_candidate,
)
from app.database.models import PromoGroup, Subscription, SubscriptionStatus, Tariff, tariff_promo_groups
from app.services.tariff_family_policy import (
    ActiveTariffFamilyConflict,
    find_live_entitlement_family_conflict,
    require_available_entitlement_family,
)
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


@pytest.mark.asyncio
async def test_precharge_guard_blocks_second_main_but_allows_renewal_and_addon(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        basic = Tariff(name='Start', entitlement_family='main')
        premium = Tariff(name='Max', entitlement_family='main')
        addon = Tariff(name='CDN', entitlement_family='metered_bypass')
        db.add_all([basic, premium, addon])
        await db.flush()

        current = _subscription(61, basic.id, SubscriptionStatus.ACTIVE)
        db.add(current)
        await db.flush()

        with pytest.raises(ActiveTariffFamilyConflict, match='смените тариф'):
            await require_available_entitlement_family(db, user_id=61, tariff_id=premium.id)

        await require_available_entitlement_family(
            db, user_id=61, tariff_id=premium.id, current_subscription_id=current.id
        )
        await require_available_entitlement_family(db, user_id=61, tariff_id=addon.id)

        # Нельзя обойти конфликт, подставив id непробной подписки.
        with pytest.raises(ActiveTariffFamilyConflict):
            await require_available_entitlement_family(
                db, user_id=61, tariff_id=premium.id, conversion_trial_id=current.id
            )


@pytest.mark.asyncio
async def test_precharge_guard_allows_only_server_resolved_trial_candidate(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        trial_tariff = Tariff(name='Trial', entitlement_family='main')
        paid_tariff = Tariff(name='Standard', entitlement_family='main')
        db.add_all([trial_tariff, paid_tariff])
        await db.flush()

        trial = _subscription(63, trial_tariff.id, SubscriptionStatus.TRIAL)
        db.add(trial)
        await db.flush()

        with pytest.raises(ActiveTariffFamilyConflict):
            await require_available_entitlement_family(db, user_id=63, tariff_id=paid_tariff.id)
        with pytest.raises(ActiveTariffFamilyConflict):
            await require_available_entitlement_family(
                db, user_id=63, tariff_id=paid_tariff.id, conversion_trial_id=999
            )
        await require_available_entitlement_family(
            db, user_id=63, tariff_id=paid_tariff.id, conversion_trial_id=trial.id
        )


@pytest.mark.asyncio
async def test_tagged_main_does_not_consume_independent_cdn_trial(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        main = Tariff(name='Main', entitlement_family='main')
        addon = Tariff(name='CDN', entitlement_family='metered_bypass')
        db.add_all([main, addon])
        await db.flush()

        cdn_trial = _subscription(65, addon.id, SubscriptionStatus.TRIAL)
        db.add(cdn_trial)
        await db.flush()

        # Покупка Main не должна конвертировать единственный живой CDN trial.
        assert await get_alive_trial_subscription(db, 65, target_family='main') is None
        assert await resolve_trial_conversion_candidate(db, 65, main.id) is None

        # И отключать trial другого семейства запрещено.
        killed = await deactivate_user_trial_subscriptions(db, 65, target_tariff_id=main.id)
        assert killed == []
        await db.refresh(cdn_trial)
        assert cdn_trial.is_trial is True
        assert cdn_trial.status == SubscriptionStatus.TRIAL.value

        # Legacy (NULL) сохраняет прежнее поведение по умолчанию.
        assert await get_alive_trial_subscription(db, 65) is cdn_trial


@pytest.mark.asyncio
async def test_tagged_main_converts_its_trial_without_disabling_addon_trial(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        main = Tariff(name='Main', entitlement_family='main')
        upgrade = Tariff(name='Main Plus', entitlement_family='main')
        addon = Tariff(name='CDN', entitlement_family='metered_bypass')
        db.add_all([main, upgrade, addon])
        await db.flush()

        main_trial = _subscription(67, main.id, SubscriptionStatus.TRIAL)
        addon_trial = _subscription(67, addon.id, SubscriptionStatus.TRIAL)
        db.add_all([main_trial, addon_trial])
        await db.flush()

        assert await resolve_trial_conversion_candidate(db, 67, upgrade.id) is main_trial

        killed = await deactivate_user_trial_subscriptions(
            db, 67, target_tariff_id=upgrade.id, exclude_subscription_id=main_trial.id
        )
        assert killed == []
        await db.refresh(addon_trial)
        assert addon_trial.is_trial is True

        # При отдельном завершении Main-trial CDN остаётся живым.
        killed = await deactivate_user_trial_subscriptions(db, 67, target_tariff_id=main.id)
        assert [item.id for item in killed] == [main_trial.id]
        await db.refresh(addon_trial)
        assert addon_trial.status == SubscriptionStatus.TRIAL.value
