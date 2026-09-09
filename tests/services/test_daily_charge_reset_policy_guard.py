"""Сторож: суточное списание нигде не решает про сброс трафика само.

Правило жило тремя копиями — планировщик, кабинет, Mini App — и во всех трёх
стояла жёсткая константа «не обнулять». Чинили бы снова по одной копии.
Здесь по разбору кода проверяется, что каждый из трёх обработчиков спрашивает
общую политику, а не подставляет константу.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

POLICY = 'should_reset_traffic_on_daily_charge'

# файл → функция, которая списывает суточную оплату и синхронизирует панель
DAILY_CHARGE_SITES = {
    'app/services/daily_subscription_service.py': '_process_single_charge',
    'app/cabinet/routes/subscription_modules/daily.py': 'toggle_subscription_pause',
    'app/webapi/routes/miniapp.py': 'toggle_daily_subscription_pause_endpoint',
}

SYNC_CALLS = {'update_remnawave_user', 'create_remnawave_user'}


def _find_function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{path}: функция {name} не найдена — сторож устарел, обновите список')


def _reset_traffic_arguments(func: ast.AST) -> list[ast.expr]:
    values: list[ast.expr] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
        if called not in SYNC_CALLS:
            continue
        for keyword in node.keywords:
            if keyword.arg == 'reset_traffic':
                values.append(keyword.value)
    return values


def test_every_daily_charge_site_asks_the_policy():
    """Каждый обработчик суточной оплаты вызывает общее правило."""
    for relative, function_name in DAILY_CHARGE_SITES.items():
        func = _find_function(ROOT / relative, function_name)
        names = {node.id for node in ast.walk(func) if isinstance(node, ast.Name)}
        assert POLICY in names, f'{relative}:{function_name} не спрашивает {POLICY}()'


def test_daily_charge_sync_never_hardcodes_reset():
    """Решение о сбросе приходит выражением, а не константой в вызове синхронизации.

    Одна константа на обработчик допустима — это досыл сквадов сразу после
    создания аккаунта, часть того же события оплаты, где обнуление уже сделано.
    """
    for relative, function_name in DAILY_CHARGE_SITES.items():
        func = _find_function(ROOT / relative, function_name)
        arguments = _reset_traffic_arguments(func)
        assert arguments, f'{relative}:{function_name}: синхронизация без reset_traffic — сторож устарел'

        computed = [value for value in arguments if not isinstance(value, ast.Constant)]
        assert computed, f'{relative}:{function_name}: все вызовы синхронизации задают reset_traffic константой'

        constants = [value for value in arguments if isinstance(value, ast.Constant)]
        assert all(value.value is False for value in constants), (
            f'{relative}:{function_name}: жёсткое reset_traffic=True в суточном списании'
        )
        assert len(constants) <= 1, (
            f'{relative}:{function_name}: больше одной константы reset_traffic — похоже, правило снова обходят'
        )
