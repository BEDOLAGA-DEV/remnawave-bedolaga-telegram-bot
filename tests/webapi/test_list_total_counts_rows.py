"""`total` в списках API — это число записей, а не «сколько поместилось на странице».

Счётчик собирался из готового запроса через `with_only_columns(func.count())`.
SQLAlchemy 2.x пересобирает при этом FROM по новым колонкам, а у `func.count()`
без аргумента колонок нет: FROM оставался только тем, что упоминал WHERE. Без
фильтров запрос вырождался в `SELECT count(*)` вообще без таблицы, и база честно
отвечала «1». Ошибки не было — `GET /users` без фильтров тихо отдавал `total: 1`,
и внешний сервис, листающий `while offset < total`, забирал одну страницу и
никогда не видел самых старых аккаунтов.

Сторож требует от каждого такого списка одного: `total` равен числу записей и
без фильтров, и с ними.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.crud.subscription_event import list_subscription_events
from app.database.models import (
    PromoGroup,
    ReferralEarning,
    Subscription,
    SubscriptionEvent,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)
from app.webapi.routes.partners import list_referrers
from app.webapi.routes.transactions import list_transactions
from app.webapi.routes.users import list_users
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    Transaction.__table__,
    SubscriptionEvent.__table__,
    PromoGroup.__table__,
    ReferralEarning.__table__,
)

NOW = datetime.now(UTC)


def _user(telegram_id: int, username: str, status: str = UserStatus.ACTIVE.value) -> User:
    return User(
        telegram_id=telegram_id,
        username=username,
        first_name=username.capitalize(),
        status=status,
        language='ru',
        balance_kopeks=0,
        referral_code=f'ref{telegram_id}',
    )


async def _seed(db) -> list[User]:
    users = [
        _user(1, 'first'),
        _user(2, 'second'),
        _user(3, 'third', status=UserStatus.BLOCKED.value),
    ]
    db.add_all(users)
    await db.flush()
    db.add_all(
        [
            Transaction(
                user_id=user.id,
                type=TransactionType.DEPOSIT.value,
                amount_kopeks=10000,
                description='Пополнение',
                is_completed=user.id != 3,
            )
            for user in users
        ]
    )
    db.add_all(
        [
            SubscriptionEvent(
                user_id=user.id,
                event_type='created' if user.id != 3 else 'expired',
                occurred_at=NOW - timedelta(minutes=user.id),
            )
            for user in users
        ]
    )
    await db.commit()
    return users


async def test_users_total_counts_everyone_without_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        page = await list_users(db=db, limit=1, offset=0, status_filter=None, promo_group_id=None, search=None)

        assert len(page.items) == 1
        assert page.total == 3


async def test_users_total_matches_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        page = await list_users(
            db=db, limit=1, offset=0, status_filter=UserStatus.ACTIVE, promo_group_id=None, search=None
        )

        assert page.total == 2


async def test_paging_by_total_reaches_the_oldest_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Клиент листает `while offset < total` — он обязан дойти до самых старых записей.

    Список идёт от новых к старым, поэтому заниженный `total` терял именно тех, кто
    зарегистрировался раньше всех.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        seen: list[str] = []
        offset = 0
        total = 1
        while offset < total:
            page = await list_users(db=db, limit=1, offset=offset, status_filter=None, promo_group_id=None, search=None)
            total = page.total
            seen.extend(item.username for item in page.items)
            offset += 1

        assert sorted(seen) == ['first', 'second', 'third']


async def test_transactions_total_counts_everyone_without_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        page = await list_transactions(
            db=db,
            limit=1,
            offset=0,
            user_id=None,
            type_filter=None,
            payment_method=None,
            is_completed=None,
            date_from=None,
            date_to=None,
        )
        assert page.total == 3

        filtered = await list_transactions(
            db=db,
            limit=1,
            offset=0,
            user_id=None,
            type_filter=None,
            payment_method=None,
            is_completed=True,
            date_from=None,
            date_to=None,
        )
        assert filtered.total == 2


async def test_subscription_events_total_counts_everyone_without_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        _, total = await list_subscription_events(db, limit=1, offset=0)
        assert total == 3

        _, filtered = await list_subscription_events(db, limit=1, offset=0, event_types=['created'])
        assert filtered == 2


async def test_referrers_total_counts_everyone_without_search(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)

        page = await list_referrers(db=db, limit=1, offset=0, search=None)
        assert page.total == 3

        found = await list_referrers(db=db, limit=1, offset=0, search='first')
        assert found.total == 1


def test_no_counter_loses_its_table() -> None:
    """Сторож на весь `app/`: счётчик страницы обязан считать по колонке.

    `with_only_columns(func.count())` — тихая ловушка: запрос компилируется, база
    отвечает, а ответ неверный ровно в том случае, когда фильтров нет. Ловим её
    разбором кода, а не глазами: тест выше проверяет только четыре известных списка.
    """
    import ast
    from pathlib import Path

    offenders: list[str] = []
    for source in Path('app').rglob('*.py'):
        tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            if not isinstance(callee, ast.Attribute) or callee.attr != 'with_only_columns':
                continue
            for argument in node.args:
                is_bare_count = (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Attribute)
                    and argument.func.attr == 'count'
                    and not argument.args
                )
                if is_bare_count:
                    offenders.append(f'{source}:{node.lineno}')

    assert not offenders, 'счётчик без колонки теряет таблицу и вернёт 1: ' + ', '.join(offenders)
