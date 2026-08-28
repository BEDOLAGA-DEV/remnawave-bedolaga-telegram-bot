"""Храповик на трактовку ``referral_earnings.referral_id``.

Строка ledger'а принадлежит ПОЛУЧАТЕЛЮ награды. Для награды пригласившему это
прежний смысл колонок; для награды приглашённому пара зеркалится, и там
``referral_id`` — уже пригласивший, а не приглашённый.

Значит любая выборка, трактующая ``referral_id`` как «приглашённый мной» —
``GROUP BY referral_id`` или ``COUNT(DISTINCT referral_id)`` — обязана отбросить
такие строки предикатом ``not_referee_directed()``. Иначе пользователь увидит в
списке своих рефералов собственного пригласившего.

Обычный тест это не ловит: он проверяет запросы, которые есть сегодня, а опасен
запрос, который допишут завтра. Поэтому здесь проверяется исходный код целиком —
как и в ратчете на web storage в кабинете.
"""

import ast
import pathlib

import pytest


APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / 'app'

# Файлы, где зеркалированные строки не искажают смысл:
#   * referral.py — здесь предикат и определён;
#   * account_merge_service — переносит строки по обеим колонкам симметрично;
#   * referral_reward_service — сам их и пишет.
_ALLOWED = {
    'database/crud/referral.py',
    'services/account_merge_service.py',
    'services/referral_reward_service.py',
}


def _iter_python_files():
    for path in sorted(APP_ROOT.rglob('*.py')):
        if 'lib/nalogo' in str(path):
            continue
        yield path


def _uses_referral_id_as_grouping(node: ast.AST) -> bool:
    """Обращение к ``.referral_id`` внутри group_by/distinct.

    Квалификатор намеренно НЕ проверяется. Привязка к литералу
    ``ReferralEarning.referral_id`` обходилась любым алиасом импорта
    (``RE.referral_id``), обращением через модуль (``models.ReferralEarning...``)
    или через ORM-алиас — то есть ровно теми формами, в которых пишут реальные
    запросы. Проверка по имени поля ловит их все; лишние срабатывания на других
    моделях снимаются разбором конкретного файла и списком исключений.
    """
    if not isinstance(node, ast.Call):
        return False

    func_name = ''
    if isinstance(node.func, ast.Attribute):
        func_name = node.func.attr
    elif isinstance(node.func, ast.Name):
        func_name = node.func.id
    if func_name not in ('group_by', 'distinct'):
        return False

    return any(isinstance(arg, ast.Attribute) and arg.attr == 'referral_id' for arg in ast.walk(node))


def _enclosing_statements(tree: ast.AST) -> dict[int, ast.stmt]:
    """Для каждого узла — ближайший объемлющий оператор.

    Считать окно в строках нельзя: запрос из десятка чейнящихся вызовов легко
    выносит предикат за любую фиксированную рамку, и храповик тихо пропускает
    ровно то, ради чего написан.
    """
    enclosing: dict[int, ast.stmt] = {}
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt):
            continue
        for child in ast.walk(statement):
            if child is statement:
                continue
            # Ближайший объемлющий: более глубокий оператор перезаписывает внешний.
            previous = enclosing.get(id(child))
            if previous is None or _spans(statement) <= _spans(previous):
                enclosing[id(child)] = statement
    return enclosing


def _spans(node: ast.stmt) -> int:
    return (getattr(node, 'end_lineno', node.lineno) or node.lineno) - node.lineno


@pytest.mark.parametrize('path', list(_iter_python_files()), ids=lambda p: str(p.relative_to(APP_ROOT)))
def test_referral_id_grouping_excludes_referee_rows(path):
    relative = str(path.relative_to(APP_ROOT))
    if relative in _ALLOWED:
        pytest.skip('файл разобран вручную')

    source = path.read_text(encoding='utf-8')
    if 'referral_id' not in source:
        return

    tree = ast.parse(source)
    enclosing = _enclosing_statements(tree)
    for node in ast.walk(tree):
        if not _uses_referral_id_as_grouping(node):
            continue
        statement = enclosing.get(id(node), node)
        context = ast.get_source_segment(source, statement) or ''
        assert 'not_referee_directed' in context, (
            f'{relative}:{node.lineno} группирует по ReferralEarning.referral_id без '
            'not_referee_directed(): строки наград приглашённому зеркалированы, и '
            'запрос засчитает пользователю его собственного пригласившего как реферала'
        )


def test_ratchet_actually_sees_the_guarded_sites():
    """Сам храповик должен что-то находить, иначе он молча деградирует в no-op."""
    found = 0
    for path in _iter_python_files():
        source = path.read_text(encoding='utf-8')
        if 'referral_id' not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if _uses_referral_id_as_grouping(node):
                found += 1
    assert found >= 2, f'ожидались известные группировки по referral_id, найдено {found}'
