"""Сторож: «сегодня», «вчера» и «по дням» нигде не считаются по UTC руками (#3136).

Репорт был про один показатель — «Доход за сегодня», — а идиом «полночь по
UTC» и «дата по UTC» по приложению оказалось за два десятка: сводки,
рефералы, промокоды, колесо, рассылки, партнёрка, статистика продаж. Каждое
такое место — тот же дефект: при Europe/Moscow платежи и регистрации с 00:00
до 02:59 уезжают во «вчера», а разные экраны расходятся между собой.

Правило одно: календарный день берётся из ``app.utils.timezone``
(``local_date`` / ``local_day_start`` / ``local_day_bounds`` /
``local_month_start``), а дата колонки в SQL — из ``app.database.local_date``.
Сторож находит нарушения разбором кода, а не списком, и проверяет сам себя
на синтетическом примере, чтобы не ослепнуть от переименования.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'app'

# Единственные места, где эти идиомы законны: сами определения.
ALLOWED_FILES = {
    'app/utils/timezone.py',
    'app/database/local_date.py',
}

MIDNIGHT_KEYWORDS = {'hour': 0, 'minute': 0, 'second': 0, 'microsecond': 0}

# Функции, где дата по UTC — по контракту или отложенный сосед, а не недосмотр (файл::функция → почему).
KNOWN_UTC_SITES = {
    'app/services/remnawave_service.py::get_top_consumers': 'диапазон дат для API панели — панель живёт в UTC',
    'app/services/remnawave_sync_service.py::_calculate_next_run': (
        'расписание синхронизации считается по UTC — соседний класс (#3030 у бэкапов), чинить отдельным коммитом'
    ),
    'app/services/traffic_monitoring_service.py::_run_daily_check_loop': (
        'суточная проверка трафика по UTC-времени — то же, чинить вместе с расписанием синхронизации'
    ),
}


def _is_zero_keywords(node: ast.Call, required: dict[str, int]) -> bool:
    given = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    if not required.keys() <= given.keys():
        return False
    for name, value in required.items():
        constant = given[name]
        if not isinstance(constant, ast.Constant) or constant.value != value:
            return False
    return True


def _is_utc_now(node: ast.AST) -> bool:
    """``datetime.now(UTC)`` / ``datetime.now(tz=UTC)`` / ``datetime.utcnow()``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr == 'utcnow':
        return True
    if node.func.attr != 'now':
        return False
    args = [*node.args, *(kw.value for kw in node.keywords)]
    return any(isinstance(arg, ast.Name) and arg.id == 'UTC' for arg in args)


def _violation(node: ast.Call) -> str | None:
    if not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr
    owner = node.func.value
    if attr == 'replace' and _is_zero_keywords(node, {'day': 1, **MIDNIGHT_KEYWORDS}):
        return 'начало месяца по UTC руками — нужен local_month_start'
    if attr == 'replace' and _is_zero_keywords(node, MIDNIGHT_KEYWORDS):
        return 'полночь по UTC руками — нужен local_day_start / local_day_bounds'
    if attr == 'date' and _is_utc_now(owner):
        return 'дата по UTC — нужен local_date'
    if attr == 'date' and isinstance(owner, ast.Name) and owner.id == 'func' and len(node.args) == 1:
        return 'func.date(колонка) без зоны — нужен local_date_expr'
    return None


def _names_derived_from_utc_now(func: ast.AST) -> set[str]:
    """Имена, которым присвоен ``datetime.now(UTC)`` или арифметика от таких имён.

    ``start_date = now - timedelta(days=30)`` — тоже UTC-момент, и ``.date()``
    от него даёт дату по UTC (подписи дней в заполняющих циклах, прод 2026-09-12).
    """
    derived: set[str] = set()
    for _ in range(2):  # второй проход подхватывает производные от производных
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and _mentions_utc_now(node.value, derived):
                derived.add(target.id)
    return derived


def _mentions_utc_now(node: ast.AST, derived: set[str]) -> bool:
    if _is_utc_now(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in derived
    if isinstance(node, ast.BinOp):
        return _mentions_utc_now(node.left, derived) or _mentions_utc_now(node.right, derived)
    return False


def _date_of_utc_moment(node: ast.Call, derived: set[str]) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != 'date' or node.args:
        return False
    return _mentions_utc_now(func.value, derived)


def find_violations(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            reason = _violation(node)
            if reason:
                found.append((node.lineno, reason))
    for func in ast.walk(tree):
        if not isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        derived = _names_derived_from_utc_now(func)
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and _date_of_utc_moment(node, derived):
                found.append((node.lineno, 'дата UTC-момента через .date() — нужен local_date'))
    return found


def _enclosing_function(tree: ast.AST, lineno: int) -> str | None:
    best: ast.AsyncFunctionDef | ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.lineno <= lineno <= (node.end_lineno or node.lineno) and (best is None or node.lineno > best.lineno):
            best = node
    return best.name if best else None


def _app_violations() -> list[str]:
    report = []
    for path in sorted(APP.rglob('*.py')):
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for lineno, reason in find_violations(tree):
            if f'{relative}::{_enclosing_function(tree, lineno)}' in KNOWN_UTC_SITES:
                continue
            report.append(f'{relative}:{lineno}: {reason}')
    return report


def test_known_utc_sites_still_exist():
    """Список исключений не должен пережить переименование: каждая функция обязана существовать."""
    for site in KNOWN_UTC_SITES:
        relative, name = site.split('::')
        tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
        assert any(
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name for node in ast.walk(tree)
        ), site


def test_detector_sees_every_idiom():
    """Сторож не ослеп: на синтетическом примере находит все пять идиом."""
    sample = ast.parse(
        'a = now.replace(hour=0, minute=0, second=0, microsecond=0)\n'
        'b = datetime.now(UTC).date()\n'
        'c = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)\n'
        'd = select(func.date(Transaction.created_at))\n'
        'ok1 = now.replace(hour=21, minute=0, second=0, microsecond=0)\n'
        'ok2 = local_date()\n'
        "ok3 = func.date(column, '+10800 seconds')\n"
        'def fill():\n'
        '    now = datetime.now(UTC)\n'
        '    start = now - timedelta(days=30)\n'
        '    e = now.date()\n'
        '    f = (start + timedelta(days=1)).date()\n'
        '    ok4 = local_date(now)\n'
    )
    reasons = [reason for _, reason in find_violations(sample)]
    assert len(reasons) == 6, reasons
    assert any('local_day_start' in r for r in reasons)
    assert any(r.split('нужен ')[-1] == 'local_date' for r in reasons)
    assert any('local_month_start' in r for r in reasons)
    assert any('local_date_expr' in r for r in reasons)
    assert sum('UTC-момента' in r for r in reasons) == 2


def test_app_has_no_hand_made_utc_days():
    violations = _app_violations()
    assert not violations, 'календарный день считается по UTC руками:\n' + '\n'.join(violations)
