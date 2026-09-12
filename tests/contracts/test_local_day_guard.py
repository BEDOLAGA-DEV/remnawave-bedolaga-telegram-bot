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


def find_violations(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            reason = _violation(node)
            if reason:
                found.append((node.lineno, reason))
    return found


def _app_violations() -> list[str]:
    report = []
    for path in sorted(APP.rglob('*.py')):
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED_FILES:
            continue
        for lineno, reason in find_violations(ast.parse(path.read_text(encoding='utf-8'))):
            report.append(f'{relative}:{lineno}: {reason}')
    return report


def test_detector_sees_every_idiom():
    """Сторож не ослеп: на синтетическом примере находит все четыре идиомы."""
    sample = ast.parse(
        'a = now.replace(hour=0, minute=0, second=0, microsecond=0)\n'
        'b = datetime.now(UTC).date()\n'
        'c = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)\n'
        'd = select(func.date(Transaction.created_at))\n'
        'ok1 = now.replace(hour=21, minute=0, second=0, microsecond=0)\n'
        'ok2 = local_date()\n'
        "ok3 = func.date(column, '+10800 seconds')\n"
    )
    reasons = [reason for _, reason in find_violations(sample)]
    assert len(reasons) == 4
    assert any('local_day_start' in r for r in reasons)
    assert any(r.split('нужен ')[-1] == 'local_date' for r in reasons)
    assert any('local_month_start' in r for r in reasons)
    assert any('local_date_expr' in r for r in reasons)


def test_app_has_no_hand_made_utc_days():
    violations = _app_violations()
    assert not violations, 'календарный день считается по UTC руками:\n' + '\n'.join(violations)
