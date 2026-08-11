"""Поиск по пользователям не должен учитывать регистр — в том числе для кириллицы.

Репорт: в админке «Поз» находит пользователя, «поз» — «Пользователи не найдены».
Причина не в SQL (там был честный ILIKE), а в локали базы: под `--locale=C`
(наш docker-compose) PostgreSQL сворачивает регистр только для ASCII. SQLite
ведёт себя так же всегда — на нём это и воспроизводится.
"""

from __future__ import annotations

import pytest

from app.database.crud.user import get_users_count, get_users_list
from app.database.models import (
    PromoGroup,
    Subscription,
    Tariff,
    User,
    UserPromoGroup,
    UserStatus,
    tariff_promo_groups,
)
from app.utils.text_search import case_variants, contains_patterns
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
)


def test_sqlite_lower_really_is_ascii_only() -> None:
    """Фиксируем причину бага: без наших вариантов ILIKE по кириллице не сработал бы."""
    import sqlite3

    connection = sqlite3.connect(':memory:')
    try:
        assert connection.execute("select lower('Поз')").fetchone()[0] == 'Поз'
        assert connection.execute("select 'Позитив' LIKE '%поз%'").fetchone()[0] == 0
        # ASCII при этом сворачивается — поэтому баг и был виден только на кириллице.
        assert connection.execute("select 'Abc' LIKE '%abc%'").fetchone()[0] == 1
    finally:
        connection.close()


def test_ascii_term_stays_a_single_pattern() -> None:
    """Для ASCII ILIKE справляется сам — лишние OR только замедлили бы запрос."""
    assert case_variants('abc') == ['abc']
    assert contains_patterns('abc') == ['%abc%']
    assert case_variants('') == ['']


def test_cyrillic_term_expands_to_case_variants() -> None:
    variants = case_variants('поз')

    assert 'поз' in variants
    assert 'Поз' in variants
    assert 'ПОЗ' in variants
    assert len(variants) == len(set(variants)), 'варианты не должны дублироваться'


def test_variants_are_deduplicated_for_single_case_terms() -> None:
    assert case_variants('ПОЗ').count('ПОЗ') == 1


async def _seed(db, names: list[str]) -> None:
    for index, name in enumerate(names):
        db.add(
            User(
                telegram_id=900000 + index,
                username=f'user{index}',
                first_name=name,
                status=UserStatus.ACTIVE.value,
                language='ru',
                balance_kopeks=0,
            )
        )
    await db.commit()


@pytest.mark.parametrize('term', ['поз', 'Поз', 'ПОЗ', 'пОз'])
async def test_search_finds_capitalized_name_in_any_case(monkeypatch: pytest.MonkeyPatch, term: str) -> None:
    """Ровно репорт: имя записано «Позитив», ищут как угодно — находиться должно всегда."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, ['Позитив', 'Другой'])

        found = await get_users_list(db, search=term)

        assert [user.first_name for user in found] == ['Позитив']
        assert await get_users_count(db, search=term) == 1


@pytest.mark.parametrize('stored', ['Позитив', 'позитив', 'ПОЗИТИВ'])
async def test_search_finds_any_stored_case(monkeypatch: pytest.MonkeyPatch, stored: str) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, [stored])

        found = await get_users_list(db, search='поз')

        assert [user.first_name for user in found] == [stored]


async def test_multiword_name_is_found_in_lowercase(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, ['Иван Петров'])

        assert len(await get_users_list(db, search='иван петров')) == 1


async def test_ascii_search_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Латиница не должна пострадать от изменения."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, ['Alexander'])

        assert len(await get_users_list(db, search='alex')) == 1
        assert len(await get_users_list(db, search='ALEX')) == 1


async def test_search_still_filters_out_non_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регистронезависимость не должна превратить поиск в «находит всё»."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, ['Позитив', 'Негатив'])

        assert [u.first_name for u in await get_users_list(db, search='нег')] == ['Негатив']
        assert await get_users_list(db, search='щщщ') == []


async def test_telegram_id_search_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, ['Позитив'])

        assert len(await get_users_list(db, search='900000')) == 1
