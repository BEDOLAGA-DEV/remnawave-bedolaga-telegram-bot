"""Сторож: продление сначала возвращает то, что оверлей грейса затёр в подписке.

v4.10–4.11 записывали оверлей грейса в подписку: дату конца грейса, сквад грейса,
лимит «расход + квота». Лечит это продление (решение владельца 2026-09-15), но
только если починка идёт ДО расчёта нового срока: опорный признак — дата конца
грейса, и как только путь её сдвинул, подписку уже не узнать (стенд: корзина
автопокупки, призы, покупки с выбором серверов считали срок от конца грейса и
оставляли сквад грейса).

По AST всего ``app/`` (детектор общий со сторожем правила трафика): функция,
которая двигает ``end_date`` вперёд или зовёт метод модели ``.extend_subscription``,
обязана либо звать CRUD ``extend_subscription`` (починка у него первой строкой),
либо звать ``undo_grace_overlay_echo`` раньше первого сдвига даты. Остальные — в
``EXEMPT`` с причиной.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib


HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parents[1] / 'app'

_spec = importlib.util.spec_from_file_location(
    'traffic_rule_guard', HERE / 'test_renewal_applies_tariff_traffic_rule.py'
)
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)

#: Двигают дату, но от текущего срока и серверов подписки ничего не берут — с причиной.
EXEMPT: dict[str, str] = {
    'app/database/models.py::Subscription.extend_subscription': 'сам метод модели — починку зовут его вызывающие',
    'app/database/crud/subscription.py::activate_pending_subscription': 'отложенная подписка грейса не получала',
    'app/database/crud/subscription.py::activate_pending_trial_subscription': 'отложенный триал грейса не получал',
    'app/database/crud/subscription.py::replace_subscription': 'замена задаёт срок от «сейчас», сквады и лимит заново',
    'app/database/crud/subscription.py::resume_daily_subscription': 'суточное возобновление: срок «сейчас + сутки»',
    'app/cabinet/routes/subscription_modules/daily.py::toggle_subscription_pause': 'суточное: срок «сейчас + сутки»',
    'app/webapi/routes/miniapp.py::toggle_daily_subscription_pause_endpoint': 'суточное: срок «сейчас + сутки»',
    'app/handlers/subscription/tariff_purchase.py::confirm_daily_tariff_purchase': (
        'суточный тариф: срок «сейчас + сутки», сквады тарифа'
    ),
    'app/handlers/subscription/tariff_purchase.py::confirm_daily_tariff_switch': (
        'переход на суточный: срок «сейчас + сутки», сквады тарифа'
    ),
    'app/services/subscription_auto_purchase_service.py::_auto_purchase_daily_tariff': (
        'суточный тариф: срок «сейчас + сутки», сквады тарифа'
    ),
}


def _first_line(node: ast.AST, predicate) -> int | None:
    lines = [child.lineno for child in ast.walk(node) if predicate(child) and hasattr(child, 'lineno')]
    return min(lines) if lines else None


def _is_call_to(child: ast.AST, name: str) -> bool:
    if not isinstance(child, ast.Call):
        return False
    func = child.func
    return (isinstance(func, ast.Name) and func.id == name) or (isinstance(func, ast.Attribute) and func.attr == name)


def _moves_date(child: ast.AST) -> bool:
    """Сам сдвиг: присвоение ``end_date`` с плюсом или вызов метода модели (не составной блок)."""
    if isinstance(child, ast.Assign | ast.AugAssign):
        return _guard._moves_end_date_forward(ast.Module(body=[child], type_ignores=[]))
    if isinstance(child, ast.Call):
        return _guard._moves_end_date_forward(ast.Module(body=[ast.Expr(child)], type_ignores=[]))
    return False


def _heals_first(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == 'extend_subscription':
            return True  # CRUD: починка — первая строка продления
    heal = _first_line(node, lambda child: _is_call_to(child, 'undo_grace_overlay_echo'))
    move = _first_line(node, _moves_date)
    return heal is not None and move is not None and heal < move


def collect() -> set[str]:
    offenders: set[str] = set()
    for path in sorted(APP.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for key, node in _guard._iter_functions(tree, path):
            if _guard._moves_end_date_forward(node) and not _heals_first(node):
                offenders.add(key)
    return offenders


def test_renewal_undoes_grace_echo_first() -> None:
    unexplained = sorted(collect() - set(EXEMPT))
    assert not unexplained, (
        'Функции сдвигают срок, не вернув затёртое оверлеем грейса (undo_grace_overlay_echo до сдвига даты):\n  '
        + '\n  '.join(unexplained)
    )


def test_exemptions_still_exist() -> None:
    stale = sorted(set(EXEMPT) - collect())
    assert not stale, 'Исключения больше не двигают дату мимо починки — уберите из списка:\n  ' + '\n  '.join(stale)


def test_detector_sees_the_recurring_gateways_heal_first() -> None:
    """Самопроверка: Lava и Platega продлевают методом модели и чинят до него."""
    for path in (APP / 'services/payment/lava.py', APP / 'services/payment/platega.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        movers = [node for _, node in _guard._iter_functions(tree, path) if _guard._moves_end_date_forward(node)]
        assert movers, f'детектор не видит продление в {path.name}'
        assert all(_heals_first(node) for node in movers), path.name
