"""Email-рассылка по промогруппе и одному пользователю (issue #3271, часть 2).

Раньше в email-рассылках кабинета были только фильтры по типу регистрации,
подписке и активности: написать всем «Продвинутым» или одному клиенту на почту
приходилось руками через API почтового провайдера.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_broadcasts
from app.database.models import (
    PromoGroup,
    ServerSquad,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
    server_squad_promo_groups,
)
from app.services import broadcast_service
from app.services.broadcast_service import email_broadcast_service, parse_email_scoped_target
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, PromoGroup.__table__, ServerSquad.__table__, server_squad_promo_groups)


def test_parse_scoped_targets() -> None:
    assert parse_email_scoped_target('promo_group_7') == ('promo_group', 7)
    assert parse_email_scoped_target('user_42') == ('user', 42)
    for bad in ('promo_group_', 'promo_group_x', 'user_0', 'user_-1', 'all_email', 'tariff_3'):
        assert parse_email_scoped_target(bad) is None
    assert admin_broadcasts._validate_email_target('promo_group_7')
    assert admin_broadcasts._validate_email_target('user_42')
    assert not admin_broadcasts._validate_email_target('user_abc')


async def _seed(db) -> dict[str, int]:
    vip = PromoGroup(name='Продвинутый', server_discount_percent=10)
    base = PromoGroup(name='Базовая')
    db.add_all([vip, base])
    await db.flush()

    def user(telegram_id: int | None, email: str | None, *, verified: bool = True, group: PromoGroup, **extra) -> User:
        values = {
            'telegram_id': telegram_id,
            'email': email,
            'email_verified': verified,
            'status': UserStatus.ACTIVE.value,
            'language': 'ru',
            'balance_kopeks': 0,
            'promo_group_id': group.id,
            **extra,
        }
        return User(**values)

    users = {
        'vip_email': user(None, 'vip@example.com', group=vip, auth_type='email'),
        'vip_tg': user(1001, 'vip-tg@example.com', group=vip),
        'vip_unverified': user(1002, 'no@example.com', verified=False, group=vip),
        'vip_blocked': user(1003, 'blocked@example.com', group=vip, status=UserStatus.BLOCKED.value),
        'base_email': user(None, 'base@example.com', group=base, auth_type='email'),
    }
    db.add_all(users.values())
    await db.commit()
    return {'vip': vip.id, 'base': base.id, **{key: value.id for key, value in users.items()}}


@asynccontextmanager
async def _db(monkeypatch: pytest.MonkeyPatch):
    async with memory_session(monkeypatch, TABLES) as db:
        bind = db.bind

        @asynccontextmanager
        async def session_factory():
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(bind, expire_on_commit=False) as session:
                yield session

        monkeypatch.setattr(broadcast_service, 'AsyncSessionLocal', session_factory)
        yield db


@pytest.mark.asyncio
async def test_promo_group_target_counts_and_fetches_the_same_people(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _db(monkeypatch) as db:
        ids = await _seed(db)
        target = f'promo_group_{ids["vip"]}'

        count = await admin_broadcasts._get_email_filter_count(db, target)
        recipients = await email_broadcast_service._fetch_email_recipients(target)

    # Только подтверждённая почта и активный статус — как у остальных фильтров.
    assert count == 2
    assert sorted(r.email for r in recipients) == ['vip-tg@example.com', 'vip@example.com']


@pytest.mark.asyncio
async def test_single_user_target_reaches_exactly_that_user(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _db(monkeypatch) as db:
        ids = await _seed(db)
        target = f'user_{ids["base_email"]}'

        assert await admin_broadcasts._get_email_filter_count(db, target) == 1
        recipients = await email_broadcast_service._fetch_email_recipients(target)
        assert [r.email for r in recipients] == ['base@example.com']

        await admin_broadcasts._ensure_email_scoped_target_exists(db, target)
        await admin_broadcasts._ensure_email_scoped_target_exists(db, 'all_email')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('key', 'status_code'),
    [('vip_unverified', 400), ('vip_blocked', 400), ('missing_user', 404), ('missing_group', 404)],
)
async def test_send_rejects_targets_nobody_can_receive(
    monkeypatch: pytest.MonkeyPatch, key: str, status_code: int
) -> None:
    async with _db(monkeypatch) as db:
        ids = await _seed(db)
        target = {
            'missing_user': 'user_99999',
            'missing_group': 'promo_group_99999',
        }.get(key) or f'user_{ids[key]}'

        with pytest.raises(HTTPException) as error:
            await admin_broadcasts._ensure_email_scoped_target_exists(db, target)

    assert error.value.status_code == status_code


@pytest.mark.asyncio
async def test_email_filters_list_promo_groups_with_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def zero(db, target):
        if target.startswith('promo_group_'):
            return await real_count(db, target)
        return 0

    real_count = admin_broadcasts._get_email_filter_count
    monkeypatch.setattr(admin_broadcasts, '_get_email_filter_count', zero)
    async with _db(monkeypatch) as db:
        ids = await _seed(db)
        response = await admin_broadcasts.get_email_filters(admin=None, db=db)

    by_key = {item.key: item for item in response.promo_group_filters}
    assert by_key[f'promo_group_{ids["vip"]}'].label == 'Продвинутый'
    assert by_key[f'promo_group_{ids["vip"]}'].count == 2
    assert by_key[f'promo_group_{ids["base"]}'].count == 1
    assert all(item.group == 'promo_group' for item in response.promo_group_filters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('target', 'statuses'),
    [
        ('active_email', (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.ACTIVE.value)),
        ('expired_email', (SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value)),
    ],
)
async def test_multi_tariff_user_gets_one_email_not_one_per_subscription(
    monkeypatch: pytest.MonkeyPatch, target: str, statuses: tuple[str, str]
) -> None:
    """Мультитариф: JOIN по подпискам размножал человека — письмо уходило по разу на подписку."""
    from datetime import UTC, datetime, timedelta

    async with memory_session(monkeypatch, (*TABLES, Tariff.__table__, Subscription.__table__)) as db:
        bind = db.bind

        @asynccontextmanager
        async def session_factory():
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(bind, expire_on_commit=False) as session:
                yield session

        monkeypatch.setattr(broadcast_service, 'AsyncSessionLocal', session_factory)
        ids = await _seed(db)
        now = datetime.now(UTC)
        for index, status in enumerate(statuses):
            db.add(
                Subscription(
                    user_id=ids['vip_email'],
                    remnawave_short_id=f'multi-{index}',
                    status=status,
                    start_date=now - timedelta(days=10),
                    end_date=now + timedelta(days=10),
                )
            )
        await db.commit()

        count = await admin_broadcasts._get_email_filter_count(db, target)
        recipients = await email_broadcast_service._fetch_email_recipients(target)

    assert [r.email for r in recipients] == ['vip@example.com']
    assert count == len(recipients)
