"""Лимит активаций партии купонов на пользователя + удаление партии.

Купоны в партии одноразовые, но их много: без лимита один человек мог
активировать всю раздачу. ``CouponBatch.max_per_user`` = 0 — прежнее поведение
без ограничения, 1 и больше — раздачи/конкурсы.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.coupon import (
    count_batch_redemptions_by_user,
    create_coupon_batch,
    delete_coupon_batch,
    get_batch_status_counts,
    get_coupon_batch_by_id,
)
from app.database.models import (
    Coupon,
    CouponBatch,
    CouponStatus,
    PromoGroup,
    Subscription,
    Tariff,
    User,
    UserPromoGroup,
    UserStatus,
    tariff_promo_groups,
)
from app.services.coupon_service import CouponRedemptionError, _check_per_user_limit
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
    CouponBatch.__table__,
    Coupon.__table__,
)


async def _seed(db, *, max_per_user: int = 0, coupons: int = 3):
    user = User(
        telegram_id=4242,
        username='u4242',
        first_name='User',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
    )
    db.add(user)
    await db.commit()

    tariff = Tariff(name='Раздача', is_active=True, device_limit=1, traffic_limit_gb=0, period_prices={'30': 10000})
    db.add(tariff)
    await db.commit()

    batch = await create_coupon_batch(
        db,
        name='Конкурс',
        tariff_id=tariff.id,
        period_days=30,
        coupons_count=coupons,
        max_per_user=max_per_user,
        valid_until=datetime.now(UTC) + timedelta(days=30),
    )
    return user, tariff, batch


async def _coupons_of(db, batch_id: int) -> list[Coupon]:
    from sqlalchemy import select

    result = await db.execute(select(Coupon).where(Coupon.batch_id == batch_id).order_by(Coupon.id))
    return list(result.scalars().all())


async def test_batch_stores_per_user_limit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, batch = await _seed(db, max_per_user=1)
        assert batch.max_per_user == 1

        stored = await get_coupon_batch_by_id(db, batch.id)
        assert stored.max_per_user == 1


async def test_zero_limit_keeps_previous_unlimited_behaviour(monkeypatch):
    """0 — прежнее поведение: сколько угодно купонов партии одному человеку."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, batch = await _seed(db, max_per_user=0)
        coupons = await _coupons_of(db, batch.id)

        for coupon in coupons:
            coupon.status = CouponStatus.REDEEMED.value
            coupon.redeemed_by = user.id
        await db.commit()

        # Даже после трёх активаций проверка не срабатывает
        fresh = await _coupons_of(db, batch.id)
        await _check_per_user_limit(db, fresh[0], user)


async def test_limit_blocks_second_activation(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, batch = await _seed(db, max_per_user=1)
        coupons = await _coupons_of(db, batch.id)

        # Первый купон свободно проходит проверку
        await _check_per_user_limit(db, coupons[0], user)

        coupons[0].status = CouponStatus.REDEEMED.value
        coupons[0].redeemed_by = user.id
        await db.commit()

        with pytest.raises(CouponRedemptionError) as exc:
            await _check_per_user_limit(db, coupons[1], user)
        assert exc.value.code == 'per_user_limit'


async def test_limit_is_per_user_not_global(monkeypatch):
    """Лимит одного пользователя не мешает другим забрать свои купоны."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, batch = await _seed(db, max_per_user=1)
        other = User(
            telegram_id=777,
            username='other',
            first_name='Other',
            status=UserStatus.ACTIVE.value,
            language='ru',
            balance_kopeks=0,
        )
        db.add(other)
        await db.commit()

        coupons = await _coupons_of(db, batch.id)
        coupons[0].status = CouponStatus.REDEEMED.value
        coupons[0].redeemed_by = user.id
        await db.commit()

        await _check_per_user_limit(db, coupons[1], other)  # не бросает


async def test_limit_counts_only_this_batch(monkeypatch):
    """Активации в другой партии не расходуют лимит текущей."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, tariff, batch = await _seed(db, max_per_user=1)
        other_batch = await create_coupon_batch(
            db, name='Другая', tariff_id=tariff.id, period_days=30, coupons_count=2, max_per_user=1
        )

        other_coupons = await _coupons_of(db, other_batch.id)
        other_coupons[0].status = CouponStatus.REDEEMED.value
        other_coupons[0].redeemed_by = user.id
        await db.commit()

        assert await count_batch_redemptions_by_user(db, batch.id, user.id) == 0
        coupons = await _coupons_of(db, batch.id)
        await _check_per_user_limit(db, coupons[0], user)  # не бросает


async def test_revoked_coupons_do_not_consume_limit(monkeypatch):
    """Считаем только реально погашенные — отозванные пользователю не достались."""
    async with memory_session(monkeypatch, TABLES) as db:
        user, _, batch = await _seed(db, max_per_user=1)
        coupons = await _coupons_of(db, batch.id)

        coupons[0].status = CouponStatus.REVOKED.value
        coupons[0].redeemed_by = user.id  # даже если поле осталось заполненным
        await db.commit()

        await _check_per_user_limit(db, coupons[1], user)  # не бросает


async def test_delete_batch_removes_batch_and_coupons(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _, _, batch = await _seed(db, coupons=3)
        batch_id = batch.id
        assert sum((await get_batch_status_counts(db, batch_id)).values()) == 3

        await delete_coupon_batch(db, batch)

        assert await get_coupon_batch_by_id(db, batch_id) is None
        assert await _coupons_of(db, batch_id) == []
