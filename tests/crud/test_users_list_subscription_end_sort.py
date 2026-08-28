"""Сортировка get_users_list по ближайшему окончанию активной подписки."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.user import get_users_list
from app.database.models import (
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
    tariff_promo_groups,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    # Нужна для подгрузки Subscription.tariff -> Tariff.promo_groups.
    tariff_promo_groups,
)


@pytest.mark.asyncio
async def test_order_by_subscription_end_soonest_first_then_no_sub(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)

        soon = User(
            telegram_id=101,
            username='soon',
            first_name='Soon',
            status=UserStatus.ACTIVE.value,
            language='ru',
        )
        later = User(
            telegram_id=102,
            username='later',
            first_name='Later',
            status=UserStatus.ACTIVE.value,
            language='ru',
        )
        no_sub = User(
            telegram_id=103,
            username='nosub',
            first_name='NoSub',
            status=UserStatus.ACTIVE.value,
            language='ru',
        )
        db.add_all([soon, later, no_sub])
        await db.commit()

        db.add_all(
            [
                Subscription(
                    user_id=soon.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    start_date=now - timedelta(days=10),
                    end_date=now + timedelta(days=2),
                    remnawave_short_id='short-soon',
                ),
                Subscription(
                    user_id=later.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    start_date=now - timedelta(days=10),
                    end_date=now + timedelta(days=30),
                    remnawave_short_id='short-later',
                ),
            ]
        )
        await db.commit()

        users = await get_users_list(db, order_by_subscription_end=True, limit=50)

        assert [u.username for u in users] == ['soon', 'later', 'nosub']


def _user(tg: int, name: str) -> User:
    return User(
        telegram_id=tg,
        username=name,
        first_name=name,
        status=UserStatus.ACTIVE.value,
        language='ru',
    )


@pytest.mark.asyncio
async def test_active_daily_subscriptions_do_not_hog_the_top(monkeypatch):
    """Суточные тарифы обязаны быть исключены — иначе сортировка бесполезна.

    У активной суточной подписки `end_date` всегда «сейчас + сутки»: каждое
    списание её двигает. Без исключения такие подписки НАВСЕГДА занимают верх
    списка и хоронят тех, ради кого сортировка и делалась, — а они как раз
    списываются с баланса сами и напоминания не требуют. Ровно этот фильтр с
    тем же обоснованием уже стоит в `get_expiring_subscriptions`.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        daily_tariff = Tariff(name='Суточный', is_daily=True, is_active=True)
        month_tariff = Tariff(name='Месячный', is_daily=False, is_active=True)
        db.add_all([daily_tariff, month_tariff])

        daily_user, month_user = _user(201, 'daily'), _user(202, 'monthly')
        db.add_all([daily_user, month_user])
        await db.commit()

        db.add_all(
            [
                Subscription(
                    user_id=daily_user.id,
                    tariff_id=daily_tariff.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    is_daily_paused=False,
                    start_date=now - timedelta(days=10),
                    end_date=now + timedelta(hours=24),  # всегда «завтра»
                    remnawave_short_id='short-daily',
                ),
                Subscription(
                    user_id=month_user.id,
                    tariff_id=month_tariff.id,
                    status=SubscriptionStatus.ACTIVE.value,
                    is_trial=False,
                    start_date=now - timedelta(days=10),
                    end_date=now + timedelta(days=2),
                    remnawave_short_id='short-month',
                ),
            ]
        )
        await db.commit()

        users = await get_users_list(db, order_by_subscription_end=True, limit=50)

        assert users[0].username == 'monthly', (
            'суточная подписка снова заняла верх — тем, кому нужно напомнить, места не осталось'
        )


@pytest.mark.asyncio
async def test_sort_follows_the_subscription_status_filter(monkeypatch):
    """Связка «покажи истёкших + отсортируй по дате» обязана работать.

    Ключ сортировки считался строго по ACTIVE, поэтому при фильтре по любому
    другому статусу он был пуст у ВСЕХ строк, и сортировка молча вырождалась
    в порядок по дате регистрации — без ошибки и без признака.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        # Регистрация в обратном порядке к датам окончания: если сортировка
        # выродится в created_at, порядок получится ровно противоположным.
        first, second = _user(301, 'expired_long_ago'), _user(302, 'expired_recently')
        # Даты регистрации в обратном порядке к датам окончания и заданы явно:
        # если сортировка выродится в `created_at DESC`, порядок будет обратным
        # ожидаемому, и тест это поймает.
        first.created_at = now - timedelta(days=90)
        second.created_at = now - timedelta(days=1)
        db.add_all([first, second])
        await db.commit()

        db.add_all(
            [
                Subscription(
                    user_id=first.id,
                    status=SubscriptionStatus.EXPIRED.value,
                    is_trial=False,
                    start_date=now - timedelta(days=60),
                    end_date=now - timedelta(days=30),
                    remnawave_short_id='short-e1',
                ),
                Subscription(
                    user_id=second.id,
                    status=SubscriptionStatus.EXPIRED.value,
                    is_trial=False,
                    start_date=now - timedelta(days=60),
                    end_date=now - timedelta(days=5),
                    remnawave_short_id='short-e2',
                ),
            ]
        )
        await db.commit()

        users = await get_users_list(
            db,
            subscription_status=SubscriptionStatus.EXPIRED.value,
            order_by_subscription_end=True,
            limit=50,
        )

        assert [u.username for u in users] == ['expired_long_ago', 'expired_recently'], (
            'сортировка не учитывает фильтр по статусу и выродилась в порядок по регистрации'
        )
