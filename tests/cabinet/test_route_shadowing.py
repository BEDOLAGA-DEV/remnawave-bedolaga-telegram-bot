"""Литеральный путь, объявленный после параметризованного, недостижим.

``GET /cabinet/admin/partners/referral-levels`` уехал в прод, отдавая 422: в том
же роутере выше был объявлен ``GET /admin/partners/{user_id}``. FastAPI берёт
первый совпавший маршрут в порядке регистрации, поэтому запрос попадал в детали
партнёра и падал на разборе ``referral-levels`` как ``int``.

Тест на регистрацию пути такое не ловит — путь-то зарегистрирован. Здесь
проверяется именно достижимость: для каждого литерального маршрута никакой более
ранний маршрут не должен совпадать с его путём.
"""

import pytest
from fastapi import FastAPI
from starlette.routing import Match


@pytest.fixture(scope='module')
def cabinet_routes():
    from app.cabinet.routes import router

    app = FastAPI()
    app.include_router(router)
    return [route for route in app.router.routes if hasattr(route, 'methods') and hasattr(route, 'path')]


def test_no_literal_route_is_shadowed(cabinet_routes):
    shadowed = []

    for index, route in enumerate(cabinet_routes):
        if '{' in route.path:
            continue  # у параметризованных путей пересечение — норма

        for method in sorted(route.methods):
            for earlier in cabinet_routes[:index]:
                if method not in earlier.methods or earlier.path == route.path:
                    continue

                match, _scope = earlier.matches({'type': 'http', 'method': method, 'path': route.path, 'headers': []})
                if match == Match.FULL:
                    shadowed.append(f'{method} {route.path} перехватывает {earlier.path}')
                    break

    assert shadowed == [], 'Литеральные пути обязаны объявляться выше параметризованных:\n' + '\n'.join(shadowed)


def test_guard_detects_shadowing(cabinet_routes):
    """Сама проверка обязана быть чувствительной, иначе она молча зелёная."""
    from app.cabinet.routes.admin_partners import router

    by_path = {route.path: route for route in router.routes if hasattr(route, 'path')}
    literal = by_path['/admin/partners/referral-levels']
    parametrised = by_path['/admin/partners/{user_id}']

    # Порядок, который уехал в прод: параметризованный маршрут первым.
    broken = [parametrised, literal]
    match, _scope = broken[0].matches({'type': 'http', 'method': 'GET', 'path': literal.path, 'headers': []})
    assert match == Match.FULL, (
        'параметризованный путь обязан совпадать с литеральным — иначе тест выше ничего не проверяет'
    )
