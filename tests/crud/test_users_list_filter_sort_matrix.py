"""Список пользователей админки: любая выборка работает с любой сортировкой.

Сортировка добавляла к запросу свой JOIN, а выборки спрашивают подписки
подзапросом «есть такая строка у этого пользователя». Когда та же таблица
оказывалась и снаружи, SQLAlchemy считала подзапрос полностью связанным с
внешним запросом, выкидывала из него все таблицы и запрос падал ещё до базы —
кабинет отвечал 500 на «Онлайн» + сортировку по трафику и ещё пять пар.

Сторож перебирает все пары «выборка × сортировка»: новая выборка или новая
сортировка обязаны работать со всеми уже существующими.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.user import get_users_count, get_users_list
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    UserStatus,
)
from app.services.connected_accounts import ConnectedAccounts
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    Transaction.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    AdvertisingCampaign.__table__,
    AdvertisingCampaignRegistration.__table__,
)

NOW = datetime.now(UTC)

#: Каждая выборка раздела «Пользователи» — в том виде, в каком её шлёт кабинет.
FILTERS: dict[str, dict] = {
    'search': {'search': 'nick'},
    'email': {'email': 'mail'},
    'status': {'status': UserStatus.ACTIVE},
    'subscription_status': {'subscription_status': SubscriptionStatus.ACTIVE.value},
    'tariff_ids': {'tariff_ids': [1]},
    'promo_group_id': {'promo_group_id': 1},
    'campaign_id': {'campaign_id': 1},
    'partner_id': {'partner_id': 1},
    'expiring': {'subscription_status': SubscriptionStatus.ACTIVE.value, 'expires_within_days': 7},
    'active_within_minutes': {'active_within_minutes': 5},
    'has_restrictions': {'has_restrictions': True},
    'without_subscription': {'has_subscription': False},
    'with_subscription': {'has_subscription': True},
    'without_purchases': {'purchase_count': 0},
    'traffic_low': {'traffic_used_percent_min': 80},
    'online': {'connected': ConnectedAccounts(panel_ids=frozenset({7001}), telegram_ids=frozenset({2}))},
    'in_grace': {'in_grace': True},
}

#: Все сортировки ручки списка.
SORTS: tuple[str, ...] = (
    'order_by_balance',
    'order_by_traffic',
    'order_by_last_activity',
    'order_by_total_spent',
    'order_by_purchase_count',
    'order_by_subscription_end',
    'order_by_grace',
)


def _user(telegram_id: int, username: str, **extra) -> User:
    return User(
        telegram_id=telegram_id,
        username=username,
        first_name=username.capitalize(),
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
        **extra,
    )


def _subscription(user: User, suffix: str, **extra) -> Subscription:
    return Subscription(
        user_id=user.id,
        status=SubscriptionStatus.ACTIVE.value,
        start_date=NOW - timedelta(days=20),
        end_date=NOW + timedelta(days=5),
        traffic_limit_gb=100,
        device_limit=1,
        remnawave_short_id=f'short{user.id}{suffix}',
        **extra,
    )


async def _seed(db) -> None:
    nick = _user(1, 'nick', email='nick@mail.com', remnawave_id=7001)
    other = _user(2, 'other')
    db.add_all([nick, other])
    await db.flush()
    db.add_all([_subscription(nick, 'a'), _subscription(other, 'b')])
    await db.commit()


@pytest.mark.parametrize('sort', SORTS)
@pytest.mark.parametrize('filter_name', list(FILTERS))
async def test_every_filter_works_with_every_sort(monkeypatch: pytest.MonkeyPatch, filter_name: str, sort: str) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        params = {**FILTERS[filter_name], sort: True}
        await get_users_list(db, **params)
        await get_users_count(db, **{k: v for k, v in params.items() if k != sort})


async def test_sorting_never_duplicates_a_user_with_several_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Мультитариф: сортировка не должна повторять человека и съедать строки страницы.

    JOIN подписок давал по строке на каждую подписку: `limit` тратился на дубли,
    страница приходила короче запрошенной, а «показано N из M» врало.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        multi = _user(11, 'multi')
        single = _user(12, 'single')
        quiet = _user(13, 'quiet')
        db.add_all([multi, single, quiet])
        await db.flush()
        # Обе подписки мультитарифа тяжелее остальных: при JOIN они займут всю
        # первую страницу вдвоём и человек на ней окажется единственным.
        for owner, suffix, used in ((multi, 'a', 90.0), (multi, 'b', 80.0), (single, 'c', 50.0), (quiet, 'd', 5.0)):
            sub = _subscription(owner, suffix)
            sub.traffic_used_gb = used
            db.add(sub)
        await db.commit()

        for sort in SORTS:
            users = await get_users_list(db, limit=2, **{sort: True})
            assert len(users) == 2, f'{sort}: страница пришла короче запрошенной'
            assert len({u.id for u in users}) == 2, f'{sort}: один человек попал в страницу дважды'

        # Сортировка по трафику ставит выше того, кто скачал больше всех.
        by_traffic = await get_users_list(db, order_by_traffic=True)
        assert [u.username for u in by_traffic] == ['multi', 'single', 'quiet']
