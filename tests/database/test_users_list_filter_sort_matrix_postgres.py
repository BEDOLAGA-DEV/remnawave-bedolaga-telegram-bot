"""Те же пары «выборка × сортировка» списка пользователей, но на настоящем PostgreSQL.

SQLite прощает многое из того, на чём прод падает: другой разбор подзапросов в
ORDER BY, свои правила для NULL и сравнения чисел. Кабинет отвечал 500 именно на
проде, поэтому сторож обязан пройти и на боевом диалекте.
"""

from __future__ import annotations

import pytest

from app.database.crud.user import get_users_count, get_users_list
from tests.crud.test_users_list_filter_sort_matrix import FILTERS, SORTS, TABLES, _seed
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('sort', SORTS)
@pytest.mark.parametrize('filter_name', list(FILTERS))
async def test_every_filter_works_with_every_sort(postgres_database: str, filter_name: str, sort: str) -> None:
    async with postgres_session(postgres_database, list(TABLES)) as db:
        await _seed(db)
        params = {**FILTERS[filter_name], sort: True}
        await get_users_list(db, **params)
        await get_users_count(db, **{k: v for k, v in params.items() if k != sort})
