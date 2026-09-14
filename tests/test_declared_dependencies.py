"""Всё, что код импортирует напрямую, должно быть объявлено в pyproject.

Пакет, приезжающий транзитивно, работает — ровно до того дня, когда соседняя
зависимость перетасует свои extras. Тогда установка проходит успешно, а бот
падает на ImportError при старте. Так и было: ``app/config.py`` импортирует
``pydantic_settings``, который приходил только через экстру ``standard`` у
FastAPI, а ``aiohttp``, ``pydantic`` и ``aiofiles`` — через aiogram.

Соответствие «модуль -> дистрибутив» берётся из метаданных установленных
пакетов (``packages_distributions``), а не из рукописной таблицы: гадать, что
``jwt`` живёт в ``pyjwt``, а ``dateutil`` в ``python-dateutil``, здесь не нужно.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('app', 'scripts')
ENTRY_POINTS = ('main.py',)

# Пакеты, которые импортируются намеренно необъявленными.
ALLOWED_UNDECLARED: dict[str, str] = {
    # Заглушки и подмены в тестах сюда не попадают: сканируется только app/.
}


def _normalize(name: str) -> str:
    """PEP 503: различия в дефисах, подчёркиваниях и регистре не значимы."""
    return name.lower().replace('_', '-').replace('.', '-')


def _declared_distributions() -> set[str]:
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    declared = set()
    for spec in data['project']['dependencies']:
        name = spec.split(';')[0].strip()
        for boundary in ('>=', '<=', '==', '!=', '~=', '>', '<', '['):
            name = name.split(boundary)[0]
        declared.add(_normalize(name.strip().strip('\'"')))
    return declared


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SOURCE_DIRS:
        files.extend((ROOT / directory).rglob('*.py'))
    files.extend(ROOT / entry for entry in ENTRY_POINTS if (ROOT / entry).exists())
    return files


def _top_level_imports() -> set[str]:
    """Корневые имена модулей, импортируемые исходниками приложения."""
    modules: set[str] = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # level > 0 — относительный импорт, это свой же код.
                if node.level == 0 and node.module:
                    modules.add(node.module.split('.')[0])
    return modules


def _third_party_distributions() -> dict[str, str]:
    """Карта «дистрибутив -> импортируемый модуль» для сторонних импортов."""
    module_to_dists = packages_distributions()
    first_party = {'app', 'scripts', 'tests', 'main'}

    found: dict[str, str] = {}
    for module in _top_level_imports():
        if module in first_party or module in sys.stdlib_module_names:
            continue
        for dist in module_to_dists.get(module, ()):
            found[_normalize(dist)] = module
    return found


def test_every_imported_package_is_declared() -> None:
    """Прямой импорт — прямая зависимость."""
    declared = _declared_distributions()
    undeclared = {
        dist: module
        for dist, module in _third_party_distributions().items()
        if dist not in declared and dist not in ALLOWED_UNDECLARED
    }

    assert not undeclared, (
        'код импортирует пакеты, не объявленные в pyproject — они держатся на '
        f'транзитивности и исчезнут молча: {undeclared}'
    )


def test_dependency_names_are_unique() -> None:
    """Один пакет не должен быть объявлен дважды с разными ограничениями."""
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    names = [
        _normalize(spec.split(';')[0].split('>')[0].split('=')[0].split('[')[0].strip())
        for spec in data['project']['dependencies']
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f'зависимость объявлена больше одного раза: {duplicates}'


@pytest.mark.parametrize('module', ['aiohttp', 'pydantic', 'pydantic_settings', 'uvicorn', 'aiofiles'])
def test_previously_transitive_imports_stay_declared(module: str) -> None:
    """Именно эти пять держались на транзитивности — закрепляем результат."""
    dist_by_module = {_normalize(d): m for d, m in _third_party_distributions().items()}
    assert module in dist_by_module.values(), f'{module} больше не импортируется — проверку можно снять'

    declared = _declared_distributions()
    owners = [dist for dist, imported in dist_by_module.items() if imported == module]
    assert any(owner in declared for owner in owners), f'{module} снова не объявлен в pyproject: {owners}'
