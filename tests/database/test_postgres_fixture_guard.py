"""Защита самой страховки: тесты на PostgreSQL не должны молча пропускаться.

Пропуск выглядит в отчёте почти как успех. Если в CI не окажется базы или
переменной окружения, весь смысл проверок блокировок исчезнет, а пайплайн
останется зелёным. Здесь проверяется, что этого не случится:

* без базы локально — честный skip;
* с ``REQUIRE_POSTGRES_TESTS=1`` — падение вместо skip;
* в CI-workflow этот флаг действительно выставлен, а база поднимается.

Сами эти проверки PostgreSQL не требуют и идут в обычном прогоне.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.postgres_db import (
    REQUIRE_POSTGRES_ENV,
    TEST_DATABASE_URL_ENV,
    postgres_dsn,
    postgres_is_required,
    require_postgres_dsn,
)


WORKFLOW_PATH = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'tests.yml'


def test_missing_url_skips_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение без PostgreSQL не должно ронять прогон."""
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv(REQUIRE_POSTGRES_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception):
        require_postgres_dsn()


def test_missing_url_fails_when_postgres_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """С поднятым флагом отсутствие базы — падение, а не пропуск."""
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, '1')

    with pytest.raises(pytest.fail.Exception) as failure:
        require_postgres_dsn()

    assert REQUIRE_POSTGRES_ENV in str(failure.value)


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes', 'on'])
def test_requirement_flag_accepts_usual_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, value)
    assert postgres_is_required() is True


@pytest.mark.parametrize('value', ['', '0', 'false', 'no', 'нет'])
def test_requirement_flag_ignores_everything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, value)
    assert postgres_is_required() is False


def test_blank_url_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая переменная — это отсутствие базы, а не адрес из пробелов."""
    monkeypatch.setenv(TEST_DATABASE_URL_ENV, '   ')
    assert postgres_dsn() is None


def test_ci_workflow_runs_postgres_tests_for_real() -> None:
    """CI обязан поднимать базу и требовать, чтобы тесты на ней прошли.

    Без этого файла достаточно убрать одну строку из workflow, и все проверки
    блокировок начнут пропускаться, не изменив цвет пайплайна.
    """
    assert WORKFLOW_PATH.exists(), 'нет workflow с тестами'
    workflow = WORKFLOW_PATH.read_text(encoding='utf-8')

    assert 'postgres:15-alpine' in workflow, 'CI не поднимает PostgreSQL'
    assert f'{TEST_DATABASE_URL_ENV}:' in workflow, 'CI не передаёт адрес тестовой базы'
    assert f"{REQUIRE_POSTGRES_ENV}: '1'" in workflow, 'CI не запрещает молчаливый пропуск тестов на PostgreSQL'
    assert 'pytest -m postgres' in workflow, 'CI не гоняет тесты на PostgreSQL отдельным шагом'
