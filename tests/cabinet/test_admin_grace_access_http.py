"""Раздел grace-доступа через настоящее приложение.

Прямой вызов обработчика возвращает объект модели и не проходит через
``response_model``: ошибка сериализации — лишнее поле, несовпавший тип, дата без
таймзоны — там не видна, а по HTTP это 500 на пустом месте. Здесь запрос идёт
через тот же роутер и те же зависимости, что в проде.
"""

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import admin_grace_access as route
from app.database.models import GraceAccessSessionModel, Subscription, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, Subscription.__table__, GraceAccessSessionModel.__table__)

VALID_UUID = '3fa85f64-5717-4562-b3fc-2c963f66afa6'
OTHER_UUID = '17b2c1de-9f47-4a3d-8c11-5b6a0f9e2d34'
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
ADMIN = SimpleNamespace(id=1, telegram_id=777)

DEFAULTS = {
    'GRACE_ACCESS_MODE': 'false',
    'GRACE_ACCESS_DURATION_HOURS': 72,
    'GRACE_ACCESS_EXPIRED_SQUAD_UUID': VALID_UUID,
    'GRACE_ACCESS_LIMITED_SQUAD_UUID': OTHER_UUID,
    'GRACE_ACCESS_EXTERNAL_SQUAD_UUID': '',
    'GRACE_ACCESS_TRAFFIC_GB': 1,
    'GRACE_ACCESS_TRIAL_ENABLED': False,
    'GRACE_ACCESS_DAILY_ENABLED': False,
    'GRACE_ACCESS_FREE_ENABLED': False,
    'GRACE_ACCESS_RECONCILE_INTERVAL_SECONDS': 60,
    'GRACE_ACCESS_RECONCILE_BATCH_SIZE': 200,
    'GRACE_ACCESS_CANDIDATE_LOOKBACK_MINUTES': 30,
}


def _override_permission_dependencies(app: FastAPI) -> None:
    """Пропустить RBAC, оставив всё остальное настоящим.

    ``require_permission`` возвращает замыкание, поэтому подменяется не имя
    зависимости, а те объекты, которые реально висят на маршрутах.
    """
    for candidate in route.router.routes:
        for dependant in candidate.dependant.dependencies:
            call = dependant.call
            # Только замыкание require_permission: в том же модуле живёт и
            # get_cabinet_db, подмена которого оставила бы обработчик без сессии.
            if getattr(call, '__name__', '') == 'dependency' and getattr(call, '__module__', '').endswith(
                'cabinet.dependencies'
            ):
                app.dependency_overrides[call] = lambda: ADMIN


@contextlib.asynccontextmanager
async def _app(monkeypatch, *, seed_session: bool = True):
    from app.config import settings
    from app.services.system_settings_service import bot_configuration_service

    for key, value in DEFAULTS.items():
        monkeypatch.setattr(settings, key, value)

    written: list[tuple[str, object]] = []

    async def fake_set_value(_db, key, value, **_kwargs):
        written.append((key, value))
        monkeypatch.setattr(settings, key, value)

    monkeypatch.setattr(bot_configuration_service, 'is_env_locked', lambda _key: False)
    monkeypatch.setattr(bot_configuration_service, 'set_value', fake_set_value)

    async with memory_session(monkeypatch, TABLES) as db:
        if seed_session:
            db.add(User(id=1, telegram_id=777, username='grace_user', first_name='Имя', language='ru'))
            db.add(
                Subscription(
                    id=1,
                    user_id=1,
                    status='active',
                    start_date=NOW - timedelta(days=30),
                    end_date=NOW,
                    remnawave_short_id='short1',
                )
            )
            db.add(
                GraceAccessSessionModel(
                    id='session-1',
                    subscription_id=1,
                    remnawave_id=1001,
                    reason='expired',
                    incident_key='incident-1',
                    state='active',
                    snapshot_version=3,
                    version=1,
                    billing_before={},
                    panel_before={},
                    overlay={},
                    started_at=NOW - timedelta(days=1),
                    grace_until=NOW + timedelta(days=2),
                    updated_at=NOW,
                    last_error='panel rejected',
                )
            )
            await db.flush()

        app = FastAPI()
        app.include_router(route.router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: db
        _override_permission_dependencies(app)

        with TestClient(app) as http:
            yield http, written, settings


@pytest.mark.asyncio
async def test_overview_serializes(monkeypatch):
    async with _app(monkeypatch) as (http, _written, _settings):
        response = http.get('/cabinet/admin/grace-access')

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['config']['mode'] == 'false'
    assert body['config']['expired_squad_uuid'] == VALID_UUID
    assert body['runtime']['configured_mode'] == 'false'
    assert body['stats']['open'] == 1
    assert body['stats']['states'] == {'active': 1}
    assert body['stats']['open_errors'] == 1
    assert body['recent_errors'][0]['last_error'] == 'panel rejected'
    assert sorted(body['restart_only']) == ['mode', 'reconcile_interval_seconds']


@pytest.mark.asyncio
async def test_sessions_serialize_dates_and_owner(monkeypatch):
    async with _app(monkeypatch) as (http, _written, _settings):
        response = http.get('/cabinet/admin/grace-access/sessions', params={'state': 'open'})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['total'] == 1
    item = body['items'][0]
    assert item['id'] == 'session-1'
    assert item['user']['telegram_id'] == 777
    # Дата приходит с зоной: без неё фронт покажет UTC как местное время.
    assert item['grace_until'].endswith('+00:00') or item['grace_until'].endswith('Z')


@pytest.mark.asyncio
async def test_put_writes_and_returns_fresh_overview(monkeypatch):
    async with _app(monkeypatch) as (http, written, _settings):
        response = http.put('/cabinet/admin/grace-access', json={'duration_hours': 48})

    assert response.status_code == 200, response.text
    assert written == [('GRACE_ACCESS_DURATION_HOURS', 48)]
    assert response.json()['config']['duration_hours'] == 48


@pytest.mark.asyncio
async def test_put_refuses_enabling_without_a_squad(monkeypatch):
    async with _app(monkeypatch) as (http, written, settings):
        monkeypatch.setattr(settings, 'GRACE_ACCESS_EXPIRED_SQUAD_UUID', '')
        response = http.put('/cabinet/admin/grace-access', json={'mode': 'true'})

    assert response.status_code == 400
    assert 'expired_squad_uuid' in response.json()['detail']
    assert written == []


@pytest.mark.asyncio
async def test_out_of_range_value_is_a_validation_error(monkeypatch):
    """422 отдаёт detail списком — фронт обязан уметь его прочитать."""
    async with _app(monkeypatch) as (http, written, _settings):
        response = http.put('/cabinet/admin/grace-access', json={'duration_hours': 99999})

    assert response.status_code == 422
    assert isinstance(response.json()['detail'], list)
    assert written == []


@pytest.mark.asyncio
async def test_explicit_null_is_refused(monkeypatch):
    """Пустое значение уезжало в system_settings как NULL и переживало перезапуск."""
    async with _app(monkeypatch) as (http, written, _settings):
        response = http.put('/cabinet/admin/grace-access', json={'duration_hours': None})

    assert response.status_code == 400
    assert 'duration_hours' in response.json()['detail']
    assert written == []


@pytest.mark.asyncio
async def test_unknown_mode_is_refused(monkeypatch):
    async with _app(monkeypatch) as (http, written, _settings):
        response = http.put('/cabinet/admin/grace-access', json={'mode': 'maybe'})

    assert response.status_code == 422
    assert written == []
