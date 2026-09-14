"""Сторож: любое продление обязано применить правило трафика тарифа.

Жалоба владельца (2026-09-13): после продления у людей появлялся безлимит,
хотя у тарифа есть лимит. Причина сидела в одной ветке общего продления, но
продлений в боте много: платёжные шлюзы (26 модулей), автоплатёж, автопокупка
после пополнения, админка бота и кабинета, внешний API, промокоды, купоны,
конкурсы, кампании, рекуррентные списания. Правило одно — база тарифа плюс
активные докупки — и живёт в CRUD (``extend_subscription`` применяет его сам,
``reconcile_tariff_traffic_limit`` — вход для тех, кто двигает дату мимо CRUD).

Сторож не список: по AST всего ``app/`` находит функции, которые двигают
``end_date`` вперёд (``end_date = ... + timedelta(...)``) или зовут метод модели
``.extend_subscription(...)``, и требует, чтобы в той же функции было одно из:

* вызов CRUD ``extend_subscription(db, ...)``;
* вызов ``reconcile_tariff_traffic_limit(...)``;
* явное присвоение ``traffic_limit_gb`` (покупка/смена тарифа сама задаёт лимит).

Остальные места перечислены в ``KNOWN_WITHOUT_RULE`` с причиной; новое место
без причины — падение теста.
"""

from __future__ import annotations

import ast
import pathlib


APP = pathlib.Path(__file__).resolve().parents[2] / 'app'

#: Функции, которые двигают дату, но правило тарифа применять не должны — с причиной.
KNOWN_WITHOUT_RULE: dict[str, str] = {
    'app/database/models.py::Subscription.extend_subscription': 'сам метод модели — правило зовут его вызывающие',
    'app/database/crud/subscription.py::extend_subscription': 'само общее продление: правило внутри (уборка лимита)',
    'app/database/crud/subscription.py::activate_pending_subscription': (
        'активация оплаченной по счёту подписки: лимит тарифа задан при её оформлении'
    ),
    'app/database/crud/subscription.py::activate_pending_trial_subscription': 'активация триала: лимит триала',
    'app/database/crud/subscription.py::resume_daily_subscription': 'суточное возобновление: лимит ведёт суточный сервис',
    'app/cabinet/routes/subscription_modules/daily.py::toggle_subscription_pause': (
        'суточная пауза/возобновление из кабинета: лимит ведёт суточный сервис'
    ),
    'app/webapi/routes/miniapp.py::toggle_daily_subscription_pause_endpoint': (
        'суточная пауза/возобновление из Mini App: лимит ведёт суточный сервис'
    ),
}


def _moves_end_date_forward(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assign | ast.AugAssign):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == 'end_date':
                    value = child.value
                    # Только движение ВПЕРЁД: `end_date -= …` (списание дней) — не продление.
                    if isinstance(child, ast.AugAssign) and isinstance(child.op, ast.Add):
                        return True
                    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
                        return True
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr == 'extend_subscription' and isinstance(child.func.value, ast.Name | ast.Attribute):
                # метод модели: subscription.extend_subscription(days)
                if not (isinstance(child.func.value, ast.Name) and child.func.value.id in {'sub_crud', 'crud'}):
                    return True
    return False


def _applies_rule(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name == 'reconcile_tariff_traffic_limit':
                return True
            if name == 'extend_subscription' and isinstance(func, ast.Name):
                return True  # CRUD-функция, не метод модели
            if name == 'extend_subscription' and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in {'sub_crud', 'crud', 'subscription_crud'}:
                    return True
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Attribute) and target.attr == 'traffic_limit_gb':
                    return True
    return False


def _function_key(path: pathlib.Path, stack: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    rel = path.relative_to(APP.parent).as_posix()
    return f'{rel}::{".".join([*stack, node.name])}'


def _iter_functions(tree: ast.AST, path: pathlib.Path):
    def visit(node: ast.AST, stack: list[str]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                yield _function_key(path, stack, child), child
                yield from visit(child, [*stack, child.name])
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, [*stack, child.name])
            else:
                yield from visit(child, stack)

    yield from visit(tree, [])


def collect_offenders() -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    for path in sorted(APP.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for key, node in _iter_functions(tree, path):
            if not _moves_end_date_forward(node):
                continue
            if _applies_rule(node):
                continue
            offenders.setdefault(key, [])
    return offenders


def test_every_renewal_applies_the_tariff_traffic_rule():
    offenders = set(collect_offenders())
    unexplained = sorted(offenders - set(KNOWN_WITHOUT_RULE))
    assert not unexplained, (
        'Функции двигают дату окончания, но не применяют правило трафика тарифа '
        '(extend_subscription CRUD / reconcile_tariff_traffic_limit / явный traffic_limit_gb):\n  '
        + '\n  '.join(unexplained)
    )


def test_known_exceptions_still_exist():
    """Список исключений не должен протухать: переименовали функцию — обнови причину."""
    offenders = set(collect_offenders())
    stale = sorted(set(KNOWN_WITHOUT_RULE) - offenders)
    assert not stale, 'Исключения больше не двигают дату мимо правила — уберите их из списка:\n  ' + '\n  '.join(stale)


def test_detector_sees_the_recurring_gateways():
    """Самопроверка детектора: рекуррентные Lava и Platega двигают дату методом модели."""
    seen: set[str] = set()
    for path in (APP / 'services/payment/lava.py', APP / 'services/payment/platega.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for key, node in _iter_functions(tree, path):
            if _moves_end_date_forward(node):
                seen.add(key)
    assert any('process_lava_subscription_callback' in key for key in seen)
    assert any('process_platega_subscription_callback' in key for key in seen)
