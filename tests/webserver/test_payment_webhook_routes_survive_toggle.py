"""Вебхук платёжки живёт, пока есть учётные данные, — а не пока включён флаг.

Флаг включения переключают из админки в рантайме (``setattr(settings, ...)``),
и меню оплаты перечитывает его на каждой отрисовке. Маршруты FastAPI при этом
фиксируются один раз при старте процесса. Из-за этого провайдер, выключенный
в момент перезапуска, после включения появлялся в списке и принимал оплату, а
его коллбек уходил в 404: платёж прошёл, зачисления нет, и в логах бота пусто —
запрос до него не доходил.

Обратная сторона тоже важна: у ненастроенного провайдера эндпоинта быть не
должно — проверять подпись коллбека там нечем.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.webserver.payments import create_payment_router


def _paths(router) -> set[str]:
    return {route.path for route in getattr(router, 'routes', []) if getattr(route, 'path', None)}


@pytest.fixture
def tribute_configured(monkeypatch):
    monkeypatch.setattr(settings, 'TRIBUTE_API_KEY', 'key', raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_WEBHOOK_PATH', '/tribute-webhook', raising=False)


def test_route_is_mounted_while_provider_is_switched_off(monkeypatch, tribute_configured) -> None:
    """Ровно сценарий из жалобы: перезапуск с выключенной платёжкой."""
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', False, raising=False)

    router = create_payment_router(SimpleNamespace(), SimpleNamespace())

    assert router is not None
    assert '/tribute-webhook' in _paths(router)


def test_route_is_mounted_when_provider_is_on(monkeypatch, tribute_configured) -> None:
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True, raising=False)

    router = create_payment_router(SimpleNamespace(), SimpleNamespace())

    assert '/tribute-webhook' in _paths(router)


def test_unconfigured_provider_has_no_endpoint(monkeypatch) -> None:
    """Без ключа подпись коллбека проверять нечем — маршрута быть не должно."""
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_API_KEY', None, raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_WEBHOOK_PATH', '/tribute-webhook', raising=False)

    router = create_payment_router(SimpleNamespace(), SimpleNamespace())

    assert '/tribute-webhook' not in _paths(router)


def test_registration_never_looks_at_the_enable_flag() -> None:
    """Ни один маршрут не должен монтироваться по is_X_enabled().

    Проверка по исходнику: следующий добавленный провайдер скопируют с
    соседнего блока, и один `is_X_enabled()` вернёт ту же дыру — молча,
    потому что на включённом инстансе всё работает.
    """
    tree = ast.parse(inspect.getsource(create_payment_router).lstrip())
    function = tree.body[0]

    # Только условия верхнеуровневых `if` — это и есть гейты регистрации.
    # Внутри самих хендлеров читать is_X_enabled() нормально: там это
    # рантайм-проверка, а не решение о монтировании маршрута.
    offenders = []
    for statement in function.body:
        if not isinstance(statement, ast.If):
            continue
        offenders += [
            node.func.attr
            for node in ast.walk(statement.test)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith('is_')
            and node.func.attr.endswith('_enabled')
        ]

    assert offenders == [], f'маршруты монтируются по флагу включения: {offenders}'


def test_auto_verification_watchdog_is_not_latched() -> None:
    """Сторож автопроверки обязан смотреть на настройку, а не на первый результат.

    ``auto_verification_active`` защёлкивал результат первой попытки: если на
    старте не был включён ни один поддерживаемый провайдер, ``start()`` выходил
    не создав задачу, и опрос статусов больше не поднимался до перезапуска.
    Вместе с незамонтированным вебхуком это оставляло включённую позже платёжку
    вообще без канала зачисления.
    """
    source = Path(__file__).resolve().parents[2].joinpath('main.py').read_text(encoding='utf-8')
    tree = ast.parse(source)

    conditions = [
        node.test
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and 'auto_payment_verification_service' in ast.dump(node)
        and "attr='start'" in ast.dump(node)
    ]
    assert conditions, 'не найден сторож автопроверки в main.py'

    for condition in conditions:
        names = {n.id for n in ast.walk(condition) if isinstance(n, ast.Name)}
        assert 'auto_verification_active' not in names, ast.dump(condition)
