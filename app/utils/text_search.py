"""Регистронезависимый поиск по тексту, не зависящий от локали базы.

`ILIKE` и `lower()` в PostgreSQL сворачивают регистр по LC_CTYPE кластера, а бот
разворачивается с `--locale=C` (см. docker-compose.yml) — под этой локалью
сворачивается ТОЛЬКО ASCII, кириллица остаётся как есть. SQLite ведёт себя так же
и всегда: его `lower()` тоже ASCII-only. Из-за этого поиск по «поз» не находил
«Позитив», хотя «Поз» находил.

Локаль уже созданной базы не поменять без dump/restore, поэтому регистр сворачиваем
на стороне приложения: подставляем в запрос несколько регистровых вариантов термина.
Ищем по именам и юзернеймам, а они на практике записаны в одной из трёх форм —
«Иван», «иван», «ИВАН»; эти формы варианты покрывают при любом регистре запроса.

ASCII-термины не трогаем: для них `ILIKE` работает и так, а лишние OR только
замедлили бы запрос.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import or_


def case_variants(term: str) -> list[str]:
    """Регистровые варианты термина, по которым имеет смысл искать.

    Для пустой строки и чистого ASCII — сам термин без изменений.
    """
    if not term or term.isascii():
        return [term]

    return list(dict.fromkeys([term, term.lower(), term.upper(), term.capitalize(), term.title()]))


def contains_patterns(term: str) -> list[str]:
    """LIKE-шаблоны «содержит term» для всех регистровых вариантов."""
    return [f'%{variant}%' for variant in case_variants(term)]


def contains_conditions(columns: Iterable[Any], term: str) -> list[Any]:
    """ILIKE-условия «колонка содержит term» — по каждой колонке и каждому варианту."""
    patterns = contains_patterns(term)
    return [column.ilike(pattern) for column in columns for pattern in patterns]


def contains_clause(columns: Iterable[Any], term: str) -> Any:
    """Готовое OR-условие «хотя бы одна колонка содержит term»."""
    return or_(*contains_conditions(columns, term))
