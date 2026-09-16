"""Список пользователей при нескольких тарифах у одного человека и на временном доступе.

Выборки по статусу подписки спрашивали «есть ли у человека ХОТЬ ОДНА такая
подписка». В мультитарифе это врало в обе стороны: человек с тремя живыми
тарифами и одной старой истёкшей попадал и в «Активные», и в «Истекшие» —
то есть в списке «кто отвалился» стояли те, кто ничего не терял.

Второе: строка показывала подписку, ближайшую к окончанию, независимо от того,
какую выборку открыли. В «Трафике на исходе» это давало полосу «0 / 600 ГБ» у
человека, попавшего туда из-за другого тарифа, забитого под завязку.

Третье: у истёкшей подписки бывает открыт временный доступ (грейс), и по списку
это было никак не отличить от простого «истекла».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.database.crud.user import get_users_count, get_users_list
from app.database.models import (
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    User,
    UserStatus,
    tariff_promo_groups,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    Transaction.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
)
NOW = datetime.now(UTC)


def _user(telegram_id: int, username: str) -> User:
    return User(
        telegram_id=telegram_id,
        username=username,
        first_name=username.capitalize(),
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
    )


def _sub(user: User, suffix: str, status: str, days: int, *, used: float = 0.0, limit: int = 100, **extra):
    return Subscription(
        user_id=user.id,
        status=status,
        start_date=NOW - timedelta(days=60),
        end_date=NOW + timedelta(days=days),
        traffic_limit_gb=limit,
        traffic_used_gb=used,
        device_limit=1,
        remnawave_short_id=f'short{user.id}{suffix}',
        **extra,
    )


async def _seed(db) -> None:
    """Владелец трёх тарифов, из которых один давно истёк, и тот, кто отвалился совсем."""
    multi = _user(1, 'multi')
    gone = _user(2, 'gone')
    db.add_all([multi, gone])
    await db.flush()
    db.add_all(
        [
            _sub(multi, 'a', SubscriptionStatus.ACTIVE.value, 26, used=0.0, limit=600),
            _sub(multi, 'b', SubscriptionStatus.ACTIVE.value, 32, used=351.8, limit=1500),
            _sub(multi, 'c', SubscriptionStatus.EXPIRED.value, -3, used=0.0, limit=300),
            _sub(gone, 'd', SubscriptionStatus.EXPIRED.value, -10, used=5.0, limit=50),
        ]
    )
    await db.commit()


async def _usernames(db, **filters) -> list[str]:
    return sorted(str(u.username) for u in await get_users_list(db, **filters))


async def test_expired_skips_anyone_who_still_has_a_live_tariff(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Истекшие» — это кто остался без доступа, а не у кого нашлась старая истёкшая строка."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        assert await _usernames(db, subscription_status='expired') == ['gone']
        assert await get_users_count(db, subscription_status='expired') == 1


async def test_active_still_finds_anyone_with_a_live_tariff(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        assert await _usernames(db, subscription_status='active') == ['multi']
        assert await get_users_count(db, subscription_status='active') == 1


async def test_limited_and_disabled_follow_the_same_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Исчерпанный трафик и отключение — тоже «доступа нет», и тоже только без живых тарифов."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        spare = _user(3, 'spare')
        stopped = _user(4, 'stopped')
        db.add_all([spare, stopped])
        await db.flush()
        db.add_all(
            [
                # У «spare» лимит выбран на одном тарифе, но второй живой — доступ есть.
                _sub(spare, 'e', SubscriptionStatus.LIMITED.value, 10, used=50.0, limit=50),
                _sub(spare, 'f', SubscriptionStatus.ACTIVE.value, 20, used=1.0, limit=100),
                _sub(stopped, 'g', SubscriptionStatus.DISABLED.value, 5),
            ]
        )
        await db.commit()

        assert await _usernames(db, subscription_status='limited') == []
        assert await _usernames(db, subscription_status='disabled') == ['stopped']


async def test_expired_tariff_of_a_live_person_still_found_by_tariff_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отбор по тарифу — про тариф, а не про доступ: истёкшая строка живого человека остаётся видна."""
    async with memory_session(monkeypatch, TABLES) as db:
        multi = _user(1, 'multi')
        db.add(multi)
        await db.flush()
        tariff = Tariff(name='Персональный', description='', is_active=True)
        db.add(tariff)
        await db.flush()
        db.add_all(
            [
                _sub(multi, 'a', SubscriptionStatus.ACTIVE.value, 26),
                _sub(multi, 'c', SubscriptionStatus.EXPIRED.value, -3, tariff_id=tariff.id),
            ]
        )
        await db.commit()

        assert await _usernames(db, tariff_ids=[tariff.id]) == ['multi']


async def _rows(db, monkeypatch: pytest.MonkeyPatch, **params):
    """Строки списка так, как их отдаёт кабинет (панель не опрашиваем)."""
    from app.cabinet.routes import admin_users
    from app.services import panel_online

    monkeypatch.setattr(panel_online, 'get_online_snapshot', AsyncMock(return_value=None))

    defaults = {
        'offset': 0,
        'limit': 50,
        'search': None,
        'email': None,
        'status': None,
        'subscription_status': None,
        'tariff_id': None,
        'promo_group_id': None,
        'campaign_id': None,
        'partner_id': None,
        'expires_within_days': None,
        'active_within_minutes': None,
        'has_restrictions': None,
        'has_subscription': None,
        'purchase_count': None,
        'traffic_used_percent_min': None,
        'online': None,
        'sort_by': admin_users.SortByEnum.CREATED_AT,
    }
    response = await admin_users.list_users(**{**defaults, **params}, admin=None, db=db)
    return response.users


async def test_row_shows_the_tariff_that_got_the_person_into_the_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Трафик на исходе»: в строке должен быть забитый тариф, а не пустой соседний."""
    async with memory_session(monkeypatch, TABLES) as db:
        multi = _user(1, 'multi')
        db.add(multi)
        await db.flush()
        roomy = Tariff(name='Семейный', description='', is_active=True)
        packed = Tariff(name='Командный', description='', is_active=True)
        db.add_all([roomy, packed])
        await db.flush()
        db.add_all(
            [
                # Ближайший к окончанию — пустой: по умолчанию строка показала бы его.
                _sub(multi, 'a', SubscriptionStatus.ACTIVE.value, 26, used=0.0, limit=600, tariff_id=roomy.id),
                _sub(multi, 'b', SubscriptionStatus.ACTIVE.value, 32, used=1400.0, limit=1500, tariff_id=packed.id),
            ]
        )
        await db.commit()

        rows = await _rows(db, monkeypatch, traffic_used_percent_min=80)

        assert [r.username for r in rows] == ['multi']
        assert rows[0].tariff_name == 'Командный'
        assert rows[0].traffic_used_gb == 1400.0
        assert rows[0].traffic_limit_gb == 1500
        # Все тарифы человека всё равно приезжают — их видно, не проваливаясь в карточку.
        assert sorted(s.tariff_name for s in rows[0].subscriptions) == ['Командный', 'Семейный']


async def test_row_shows_the_expired_tariff_in_the_expired_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        gone = _user(2, 'gone')
        db.add(gone)
        await db.flush()
        stopped_tariff = Tariff(name='Отключённый', description='', is_active=True)
        lapsed_tariff = Tariff(name='Истёкший', description='', is_active=True)
        db.add_all([stopped_tariff, lapsed_tariff])
        await db.flush()
        db.add_all(
            [
                _sub(gone, 'a', SubscriptionStatus.DISABLED.value, -1, tariff_id=stopped_tariff.id),
                _sub(gone, 'b', SubscriptionStatus.EXPIRED.value, -10, tariff_id=lapsed_tariff.id),
            ]
        )
        await db.commit()

        rows = await _rows(db, monkeypatch, subscription_status='expired')

        assert [r.tariff_name for r in rows] == ['Истёкший']


async def test_row_says_the_person_is_on_temporary_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Грейс: подписка истекла, но доступ ещё открыт — по списку это обязано быть видно."""

    grace_until = NOW + timedelta(days=2)

    async with memory_session(monkeypatch, TABLES) as db:
        on_grace = _user(3, 'ongrace')
        plain = _user(4, 'plain')
        db.add_all([on_grace, plain])
        await db.flush()
        db.add_all(
            [
                _sub(
                    on_grace,
                    'a',
                    SubscriptionStatus.EXPIRED.value,
                    -1,
                    grace_session_open=True,
                    grace_overlay_expire_at=grace_until,
                ),
                _sub(plain, 'b', SubscriptionStatus.EXPIRED.value, -1),
            ]
        )
        await db.commit()

        rows = {r.username: r for r in await _rows(db, monkeypatch, subscription_status='expired')}

        assert rows['ongrace'].grace_until == grace_until
        assert rows['plain'].grace_until is None
        assert rows['ongrace'].subscriptions[0].grace_until == grace_until


async def test_closed_grace_leaves_no_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Дата оверлея остаётся в подписке и после закрытия грейса — это не «временный доступ»."""
    async with memory_session(monkeypatch, TABLES) as db:
        closed = _user(5, 'closed')
        db.add(closed)
        await db.flush()
        db.add(
            _sub(
                closed,
                'a',
                SubscriptionStatus.EXPIRED.value,
                -1,
                grace_session_open=False,
                grace_overlay_expire_at=NOW - timedelta(days=1),
            )
        )
        await db.commit()

        rows = await _rows(db, monkeypatch, subscription_status='expired')

        assert rows[0].grace_until is None
