"""Сторож: суточное списание нигде не решает про сброс трафика само.

Правило жило копиями, и во всех стояла жёсткая константа «не обнулять». Первый
фикс закрыл три копии по рукописному списку — и пропустил ещё две (кнопку
«возобновить» в самом боте и авто-возобновление после пополнения), потому что
список писался руками. Теперь места суточной оплаты сторож находит сам, по
разбору кода всего приложения: функция, которая синхронизирует панель и при
этом проводит транзакцию с описанием суточного списания, — это оно.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'app'

POLICY = 'should_reset_traffic_on_daily_charge'

# Описание транзакции, с которым проводится каждое суточное списание. Покупка и
# смена тарифа проводятся с другими описаниями — они живут по правилам покупки.
CHARGE_DESCRIPTION_MARKER = 'Суточная оплата'

SYNC_CALLS = {'update_remnawave_user', 'create_remnawave_user'}

# Все известные места. Детектор обязан находить как минимум их — иначе он ослеп
# (переименовали функцию или описание транзакции), и сторож надо обновить.
KNOWN_SITES = {
    'app/services/daily_subscription_service.py::_process_single_charge',
    'app/cabinet/routes/subscription_modules/daily.py::toggle_subscription_pause',
    'app/webapi/routes/miniapp.py::toggle_daily_subscription_pause_endpoint',
    'app/handlers/subscription/purchase.py::handle_toggle_daily_subscription_pause',
    'app/services/subscription_auto_purchase_service.py::try_resume_disabled_daily_after_topup',
}


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, 'id', None)


def _string_constants(func: ast.AST) -> list[str]:
    return [node.value for node in ast.walk(func) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _is_daily_charge_site(func: ast.AST) -> bool:
    calls = {_called_name(node) for node in ast.walk(func) if isinstance(node, ast.Call)}
    if not calls & SYNC_CALLS:
        return False
    return any(CHARGE_DESCRIPTION_MARKER in text for text in _string_constants(func))


def _daily_charge_sites() -> dict[str, ast.AST]:
    sites: dict[str, ast.AST] = {}
    for path in sorted(APP.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if _is_daily_charge_site(node):
                sites[f'{path.relative_to(ROOT)}::{node.name}'] = node
    return sites


def _reset_traffic_arguments(func: ast.AST) -> list[ast.expr]:
    values: list[ast.expr] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or _called_name(node) not in SYNC_CALLS:
            continue
        for keyword in node.keywords:
            if keyword.arg == 'reset_traffic':
                values.append(keyword.value)
    return values


def test_detector_still_sees_every_known_site():
    """Детектор не ослеп: каждое известное место суточной оплаты он находит сам."""
    found = set(_daily_charge_sites())
    missing = KNOWN_SITES - found
    assert not missing, f'детектор не видит известные места суточной оплаты: {sorted(missing)}'


def test_every_daily_charge_site_asks_the_policy():
    """Каждое место суточной оплаты — найденное, а не перечисленное — спрашивает общее правило."""
    offenders = []
    for site, func in _daily_charge_sites().items():
        names = {node.id for node in ast.walk(func) if isinstance(node, ast.Name)}
        if POLICY not in names:
            offenders.append(site)
    assert not offenders, f'суточная оплата без {POLICY}(): {sorted(offenders)}'


def test_daily_charge_sync_never_hardcodes_reset():
    """Решение о сбросе приходит выражением, а не константой в вызове синхронизации.

    Одна константа на обработчик допустима — это досыл сквадов сразу после
    создания аккаунта, часть того же события оплаты, где обнуление уже сделано.
    """
    for site, func in _daily_charge_sites().items():
        arguments = _reset_traffic_arguments(func)
        assert arguments, f'{site}: синхронизация без reset_traffic — сторож устарел'

        computed = [value for value in arguments if not isinstance(value, ast.Constant)]
        assert computed, f'{site}: все вызовы синхронизации задают reset_traffic константой'

        constants = [value for value in arguments if isinstance(value, ast.Constant)]
        assert all(value.value is False for value in constants), (
            f'{site}: жёсткое reset_traffic=True в суточном списании'
        )
        assert len(constants) <= 1, f'{site}: больше одной константы reset_traffic — похоже, правило снова обходят'
