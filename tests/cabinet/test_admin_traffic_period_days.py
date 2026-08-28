"""Длина периода считается там же, где разобраны даты.

В ``export_traffic_csv`` ``start_dt``/``end_dt`` присваивались под
``if request.start_date and request.end_date`` и читались на 60 строк ниже под
ВТОРЫМ, скопированным экземпляром того же условия. Пока копии совпадают, всё
работает — но правка одной из них даёт ``UnboundLocalError`` и 500 прямо на
выгрузке CSV, а статический анализ видит это как обращение к возможно
неинициализированной переменной (два error-алерта CodeQL).
"""

from __future__ import annotations

import ast
import inspect

from app.cabinet.routes.admin_traffic import export_traffic_csv


def _function_ast() -> ast.AsyncFunctionDef:
    return ast.parse(inspect.getsource(export_traffic_csv).lstrip()).body[0]


def test_dates_are_read_only_where_they_are_assigned() -> None:
    """Чтение start_dt/end_dt не должно жить вне ветки, которая их задаёт."""
    function = _function_ast()

    assigning_branches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and any(
            isinstance(target, ast.Name) and target.id in {'start_dt', 'end_dt'}
            for sub in ast.walk(node)
            if isinstance(sub, ast.Assign)
            for target in sub.targets
        )
    ]
    assert assigning_branches, 'не найдена ветка, присваивающая start_dt/end_dt'

    inside = {id(node) for branch in assigning_branches for node in ast.walk(branch)}
    stray = [
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id in {'start_dt', 'end_dt'} and id(node) not in inside
    ]

    assert stray == [], f'start_dt/end_dt читаются вне присваивающей ветки: {stray}'


def test_period_days_is_set_in_every_branch() -> None:
    """Обе ветки разбора дат обязаны задать period_days.

    Иначе он снова уедет в отдельный `if` с продублированным условием.
    """
    function = _function_ast()

    branches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and node.orelse
        and any(
            isinstance(target, ast.Name) and target.id == 'period_days'
            for sub in node.body
            if isinstance(sub, ast.Assign)
            for target in sub.targets
        )
    ]
    assert branches, 'period_days не задаётся в основной ветке разбора дат'

    for branch in branches:
        assigned_in_else = {
            target.id
            for sub in branch.orelse
            if isinstance(sub, ast.Assign)
            for target in sub.targets
            if isinstance(target, ast.Name)
        }
        assert 'period_days' in assigned_in_else, 'ветка else не задаёт period_days'
