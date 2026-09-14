"""Сторож: сериализация «доступности» читает у хоста и ноды панели только те поля,
которые есть у датаклассов клиента.

Регрессия 2026-09-10: аудит клиента против OpenAPI 3.4.3 переименовал у хоста
``tag`` в ``tags``; потребитель в кабинете остался старым, и список хостов падал
на проде AttributeError. Тесты роута строили хост через SimpleNamespace и этого не
видели. Здесь — сверка по AST: каждое ``view.host.<поле>`` и ``view.node.<поле>``
в роуте обязано существовать у ``RemnaWaveHost`` / ``RemnaWaveNode``.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from app.external.remnawave_api import RemnaWaveHost, RemnaWaveNode


ROUTE = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'admin_reachability.py'
MODELS = {'host': RemnaWaveHost, 'node': RemnaWaveNode}


def _panel_field_reads(source: str) -> list[tuple[str, str, int]]:
    """(модель, поле, строка) для каждого ``view.<модель>.<поле>``."""
    reads: list[tuple[str, str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == 'view'
            and owner.attr in MODELS
        ):
            reads.append((owner.attr, node.attr, node.lineno))
    return reads


def test_route_reads_only_fields_that_exist_on_panel_dataclasses() -> None:
    reads = _panel_field_reads(ROUTE.read_text(encoding='utf-8'))
    assert reads, 'роут перестал читать поля хоста/ноды — сторож больше ничего не проверяет'

    known = {name: {f.name for f in fields(model)} for name, model in MODELS.items()}
    stale = [(model, attr, line) for model, attr, line in reads if attr not in known[model]]
    assert not stale, 'роут читает поля, которых нет у датакласса панели (переименованы при аудите?): ' + ', '.join(
        f'view.{model}.{attr} (строка {line})' for model, attr, line in stale
    )
