"""Статическая защита от NameError на `texts` в хендлерах.

Локализация переводит хендлеры на `texts.t(...)`, но `texts` — локальная
переменная, которую каждая функция обязана получить сама через `get_texts(...)`.
Пропущенное присваивание не ловится ни линтером, ни импортом: падает только
в рантайме, у живого пользователя, и часто уже ПОСЛЕ списания денег.

Так уже случилось в confirm_tariff_extend (продление тарифа): деньги списаны,
подписка продлена, а сообщение об успехе падало с NameError; except-ветка
обращалась к тому же `texts` и падала повторно, поэтому пользователь не видел
вообще ничего и мог оплатить второй раз.

Проверка учитывает замыкания: вложенная функция законно читает `texts`
из объемлющей области, если та его определила.
"""

from __future__ import annotations

import ast
from pathlib import Path


HANDLERS_ROOT = Path(__file__).resolve().parents[2] / 'app' / 'handlers'

_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_own_scope(node: ast.AST):
    """Обходит тело узла, не заходя во вложенные области видимости."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES):
            continue
        yield child
        yield from _walk_own_scope(child)


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split('.')[0])
    return names


def _own_bound_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Имена, связанные непосредственно в этой функции (без вложенных)."""
    bound: set[str] = set()

    args = func.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        bound.add(arg.arg)
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)

    for node in _walk_own_scope(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)

    # Вложенные функции видны по имени в этой области
    for child in ast.iter_child_nodes(func):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)

    return bound


def _check_scope(node: ast.AST, inherited: set[str], found: list[tuple[str, int]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            available = inherited | _own_bound_names(child)
            loads = [
                sub.lineno
                for sub in _walk_own_scope(child)
                if isinstance(sub, ast.Name) and sub.id == 'texts' and isinstance(sub.ctx, ast.Load)
            ]
            if loads and 'texts' not in available:
                found.append((child.name, min(loads)))
            _check_scope(child, available, found)
        else:
            _check_scope(child, inherited, found)


def _find_undefined_texts(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found: list[tuple[str, int]] = []
    _check_scope(tree, _module_level_names(tree), found)
    return found


def test_handlers_never_use_undefined_texts() -> None:
    problems: list[str] = []

    for path in sorted(HANDLERS_ROOT.rglob('*.py')):
        for func_name, lineno in _find_undefined_texts(path):
            rel = path.relative_to(HANDLERS_ROOT.parents[1])
            problems.append(f'{rel}:{lineno} — {func_name}() использует texts, но не получает его через get_texts()')

    assert not problems, 'Найдены хендлеры с необъявленным `texts`:\n' + '\n'.join(problems)
