"""Копии тарифа на настоящем PostgreSQL: что держит частичный индекс, а что код.

Индекс ``uq_subscriptions_user_tariff_active`` по-прежнему не пускает вторую
живую обычную подписку тарифа (гонка двух обычных покупок), а копии со своим
``copy_key`` живут рядом. Повтор той же покупки копии упирается в уникальность
ключа.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.crud import subscription as sub_crud
from app.database.models import Base, Subscription, Tariff, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

NOW = datetime.now(UTC)


def _row(sub_id: int, *, tariff_id: int = 1, status: str = 'active', copy_key: str | None = None) -> Subscription:
    return Subscription(
        id=sub_id,
        user_id=1,
        tariff_id=tariff_id,
        remnawave_short_id=f'copy-{sub_id}',
        status=status,
        is_trial=False,
        start_date=NOW - timedelta(days=1),
        end_date=NOW + timedelta(days=30) if status == 'active' else NOW - timedelta(days=1),
        device_limit=1,
        copy_key=copy_key,
    )


async def _seed(db) -> None:
    db.add(User(id=1, telegram_id=101, language='ru', status='active'))
    db.add_all(
        [
            Tariff(id=1, name='Premium', is_active=True, device_limit=1, period_prices={'30': 300}),
            Tariff(id=2, name='Basic', is_active=True, device_limit=1, period_prices={'30': 100}),
        ]
    )
    await db.commit()


async def test_index_still_refuses_a_second_ordinary_live_subscription(postgres_database):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add(_row(10))
        await db.commit()

        db.add(_row(11))
        with pytest.raises(IntegrityError) as error:
            await db.commit()
        assert sub_crud._is_trial_unique_violation(error.value)


async def test_copies_live_next_to_the_ordinary_subscription(postgres_database):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add_all([_row(10), _row(11, copy_key='a' * 32), _row(12, copy_key='b' * 32)])
        await db.commit()

        live = await sub_crud.get_live_subscriptions_by_user_and_tariff(db, 1, 1)
        assert sorted(subscription.id for subscription in live) == [10, 11, 12]


async def test_replaying_the_same_copy_purchase_hits_the_key(postgres_database):
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add_all([_row(10), _row(11, copy_key='a' * 32)])
        await db.commit()

        db.add(_row(12, copy_key='a' * 32))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_create_paid_subscription_makes_a_copy_only_on_request(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add(_row(10, copy_key='a' * 32))  # живёт только копия
        await db.commit()

        # Обычная покупка рядом с живой копией: тот же отказ, что давал индекс.
        with pytest.raises(sub_crud.TariffAlreadyActiveError):
            await sub_crud.create_paid_subscription(
                db, user_id=1, duration_days=30, tariff_id=1, connected_squads=['s']
            )
        await db.rollback()

        copy = await sub_crud.create_paid_subscription(
            db, user_id=1, duration_days=30, tariff_id=1, connected_squads=['s'], copy_key='c' * 32
        )
        assert copy.copy_key == 'c' * 32
        live = await sub_crud.get_live_subscriptions_by_user_and_tariff(db, 1, 1)
        assert len(live) == 2


async def test_copy_purchase_revives_the_expired_row_of_the_tariff(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add_all([_row(10), _row(11, status='expired')])
        await db.commit()

        revived = await sub_crud.create_paid_subscription(
            db, user_id=1, duration_days=30, tariff_id=1, connected_squads=['s'], copy_key='d' * 32
        )

        assert revived.id == 11
        assert revived.status == 'active'
        assert revived.copy_key == 'd' * 32
        rows = (await db.execute(select(Subscription).where(Subscription.user_id == 1))).scalars().all()
        assert len(rows) == 2  # ни одной лишней строки


async def test_switching_into_an_owned_tariff_becomes_a_copy(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add_all([_row(10, tariff_id=1), _row(20, tariff_id=2)])
        await db.commit()

        moving = await db.get(Subscription, 20)
        assert await sub_crud.ensure_tariff_copy_key(db, moving, 1) is True
        moving.tariff_id = 1
        await db.commit()

        live = await sub_crud.get_live_subscriptions_by_user_and_tariff(db, 1, 1)
        assert sorted(subscription.id for subscription in live) == [10, 20]


async def test_account_merge_keeps_both_subscriptions_of_the_tariff(postgres_database, monkeypatch):
    """Оба аккаунта платили за тариф — после объединения живут обе подписки."""
    from unittest.mock import AsyncMock

    from sqlalchemy.orm import selectinload

    from app.services import account_merge_service

    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(account_merge_service, '_sync_transferred_subscriptions_to_panel', AsyncMock())
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add(User(id=2, telegram_id=202, language='ru', status='active'))
        db.add(_row(10))
        secondary_row = _row(20)
        secondary_row.user_id = 2
        db.add(secondary_row)
        await db.commit()

        users = {
            user.id: user
            for user in (
                await db.execute(select(User).options(selectinload(User.subscriptions)).where(User.id.in_([1, 2])))
            ).scalars()
        }
        await account_merge_service._handle_subscription_merge(db, users[1], users[2], 'primary', [])
        await db.commit()

        live = await sub_crud.get_live_subscriptions_by_user_and_tariff(db, 1, 1)
        assert sorted(subscription.id for subscription in live) == [10, 20]
        moved = await db.get(Subscription, 20)
        assert moved.copy_key is not None


async def test_single_tariff_mode_keeps_the_old_refusal(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        await _seed(db)
        db.add_all([_row(10, tariff_id=1), _row(20, tariff_id=2)])
        await db.commit()

        moving = await db.get(Subscription, 20)
        assert await sub_crud.ensure_tariff_copy_key(db, moving, 1) is False
        moving.tariff_id = 1
        with pytest.raises(IntegrityError):
            await db.commit()
