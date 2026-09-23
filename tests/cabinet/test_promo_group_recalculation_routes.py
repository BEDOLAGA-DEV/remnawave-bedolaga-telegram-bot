"""Кнопка «Пересчитать участников» в разделе «Группы скидок» кабинета.

Пересчёт идёт в фоне — POST ставит проход и сразу отвечает состоянием, GET
отдаёт то же состояние, чтобы кабинет опрашивал его, пока идёт проход, и
обновил счётчики участников по окончании.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes import admin_promocodes as route
from app.services.promo_group_recalculation import RecalculationResult


ADMIN = SimpleNamespace(id=1, telegram_id=1)
PATH = '/admin/promo-groups/recalculate'


def _first_full_match(method: str, path: str):
    for candidate in route.promo_groups_router.routes:
        if method not in candidate.methods:
            continue
        match, _scope = candidate.matches({'type': 'http', 'method': method, 'path': path, 'headers': []})
        if match.name == 'FULL':
            return candidate
    raise AssertionError(f'{method} {path} не совпал ни с одним маршрутом')


def _required_permissions(endpoint_name: str) -> set[str]:
    """Права из зависимостей маршрута: ``require_permission`` хранит их в замыкании."""
    for candidate in route.promo_groups_router.routes:
        if candidate.endpoint.__name__ != endpoint_name:
            continue
        found: set[str] = set()
        for dependant in candidate.dependant.dependencies:
            for cell in getattr(dependant.call, '__closure__', None) or ():
                value = cell.cell_contents
                if isinstance(value, tuple) and value and all(isinstance(item, str) and ':' in item for item in value):
                    found.update(value)
        return found
    raise AssertionError(f'Маршрут {endpoint_name} не найден')


def test_routes_registered(registered_paths):
    assert registered_paths['/cabinet' + PATH] == {'GET', 'POST'}


def test_status_url_is_not_swallowed_by_the_group_id_route():
    """``/{group_id}`` объявлен раньше и принимает любую строку — «recalculate» дал бы 422."""
    assert _first_full_match('GET', PATH).endpoint.__name__ == 'get_promo_group_recalculation_status'
    assert _first_full_match('POST', PATH).endpoint.__name__ == 'start_promo_group_recalculation'


def test_permissions_follow_the_action():
    assert _required_permissions('start_promo_group_recalculation') == {'promo_groups:edit'}
    assert _required_permissions('get_promo_group_recalculation_status') == {'promo_groups:read'}


@pytest.fixture
def recalculation(monkeypatch):
    from app.services.promo_group_recalculation import promo_group_recalculation

    reasons: list[str] = []
    state = SimpleNamespace(reasons=reasons, running=False, started=True)
    monkeypatch.setattr(promo_group_recalculation, 'schedule', lambda reason: reasons.append(reason) or state.started)
    monkeypatch.setattr(
        promo_group_recalculation,
        'snapshot',
        lambda: {
            'running': state.running,
            'reason': reasons[-1] if reasons else None,
            'queued': False,
            'last': RecalculationResult(reason='старый', checked=5913, changed=42).to_dict(),
        },
    )
    return state


@pytest.mark.asyncio
async def test_post_starts_a_pass_and_returns_its_state(recalculation):
    recalculation.running = True

    response = await route.start_promo_group_recalculation(admin=ADMIN)

    assert recalculation.reasons == ['запущен из кабинета']
    assert (response.started, response.running) == (True, True)
    assert (response.last.checked, response.last.changed) == (5913, 42)


@pytest.mark.asyncio
async def test_post_during_a_pass_reports_it_was_queued(recalculation):
    recalculation.started = False
    recalculation.running = True

    response = await route.start_promo_group_recalculation(admin=ADMIN)

    assert response.started is False
    assert response.running is True


@pytest.mark.asyncio
async def test_get_reports_state_without_starting_anything(recalculation):
    response = await route.get_promo_group_recalculation_status(admin=ADMIN)

    assert recalculation.reasons == []
    assert response.running is False
    assert response.last.reason == 'старый'
