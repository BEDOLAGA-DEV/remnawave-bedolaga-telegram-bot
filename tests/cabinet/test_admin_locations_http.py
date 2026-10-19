"""Маршруты блока «Локации» через настоящий роутер: приём отчётов и выдача карточек."""

import contextlib
import fnmatch
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cabinet.routes import admin_locations as route
from app.services.location_presence import devices_from_report
from tests.fixtures.locations import (
    OVERRIDES,
    SKIP_MARKERS,
    TAG_AUTO,
    TAG_DE,
    TAG_NL,
    sample_hosts,
    sample_nodes,
)


TOKEN = 'secret-token'
HEADERS = {'X-Location-Counter-Token': TOKEN}
ADMIN = SimpleNamespace(id=1, telegram_id=777)


class FakeCache:
    def __init__(self, *, connected: bool = True):
        self.connected = connected
        self.store: dict[str, object] = {}
        self.expires: dict[str, int | None] = {}

    async def set(self, key, value, expire=None):
        if not self.connected:
            return False
        self.store[key] = value
        self.expires[key] = expire
        return True

    async def get(self, key):
        return self.store.get(key)

    async def get_keys(self, pattern='*'):
        return [key for key in self.store if fnmatch.fnmatch(key, pattern)]


class FakeApi:
    async def get_all_hosts(self):
        return sample_hosts()

    async def get_all_nodes(self):
        return sample_nodes()


class FakeService:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    @contextlib.asynccontextmanager
    async def get_api_client(self):
        if self.fail:
            raise RuntimeError('panel is down')
        yield FakeApi()


def _override_permission_dependencies(app: FastAPI) -> None:
    for candidate in route.router.routes:
        for dependant in candidate.dependant.dependencies:
            call = dependant.call
            if getattr(call, '__name__', '') == 'dependency' and getattr(call, '__module__', '').endswith(
                'cabinet.dependencies'
            ):
                app.dependency_overrides[call] = lambda: ADMIN


@contextlib.contextmanager
def _client(monkeypatch, *, token: str = TOKEN, panel_fails: bool = False, cache_connected: bool = True):
    fake_cache = FakeCache(connected=cache_connected)
    monkeypatch.setattr(route, 'cache', fake_cache)
    monkeypatch.setattr(route.settings, 'LOCATION_COUNTER_TOKEN', token)
    monkeypatch.setattr(
        route.settings, 'LOCATION_HOST_NODE_OVERRIDES', ','.join(f'{k}={v}' for k, v in OVERRIDES.items())
    )
    monkeypatch.setattr(route.settings, 'LOCATION_EXIT_BREAKDOWN_INBOUNDS', TAG_AUTO)
    monkeypatch.setattr(route.settings, 'LOCATION_SKIP_HOST_MARKERS', SKIP_MARKERS)
    monkeypatch.setattr(route, 'RemnaWaveService', lambda: FakeService(fail=panel_fails))

    app = FastAPI()
    app.include_router(route.router, prefix='/cabinet')
    _override_permission_dependencies(app)
    with TestClient(app) as http:
        yield http, fake_cache


def _iso(moment: datetime) -> str:
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def _report(node: str, devices: list[dict], generated_at: datetime | None = None) -> dict:
    return {
        'node': node,
        'generated_at': _iso(generated_at or datetime.now(UTC)),
        'window_seconds': 300,
        'devices': devices,
    }


def _device(seconds_ago: int, inbound: str, *, exit_tag: str | None = None, user: str = 'u1', device: str = 'd1'):
    return {
        'u': user,
        'd': device,
        't': _iso(datetime.now(UTC) - timedelta(seconds=seconds_ago)),
        'inbound': inbound,
        'exit': exit_tag,
    }


def test_report_rejected_when_token_unset(monkeypatch):
    with _client(monkeypatch, token='') as (http, fake_cache):
        response = http.post('/cabinet/admin/stats/locations/report', json=_report('HUB', []), headers=HEADERS)

    assert response.status_code == 404
    assert fake_cache.store == {}


def test_report_rejected_with_wrong_token(monkeypatch):
    with _client(monkeypatch) as (http, fake_cache):
        wrong = http.post(
            '/cabinet/admin/stats/locations/report',
            json=_report('HUB', []),
            headers={'X-Location-Counter-Token': 'nope'},
        )
        missing = http.post('/cabinet/admin/stats/locations/report', json=_report('HUB', []))
        # Неверный токен важнее битого тела: незнакомцу не показываем детали схемы.
        wrong_and_broken = http.post(
            '/cabinet/admin/stats/locations/report',
            json={'node': ''},
            headers={'X-Location-Counter-Token': 'nope'},
        )

    assert wrong.status_code == 403
    assert missing.status_code == 403
    assert wrong_and_broken.status_code == 403
    assert fake_cache.store == {}


def test_report_stored_with_ttl(monkeypatch):
    body = _report('HUB', [_device(10, TAG_DE, exit_tag='FI2')])
    with _client(monkeypatch) as (http, fake_cache):
        response = http.post('/cabinet/admin/stats/locations/report', json=body, headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {'ok': True}
    assert fake_cache.expires['location_report:HUB'] == 180
    # Сохранённый отчёт читается тем же разбором, что и при выдаче.
    stored = fake_cache.store['location_report:HUB']
    devices = devices_from_report(stored)
    assert [(d.user_key, d.device_key, d.inbound, d.exit) for d in devices] == [('u1', 'd1', TAG_DE, 'FI2')]
    assert devices[0].last_seen.tzinfo is not None


@pytest.mark.parametrize(
    'mutate',
    [
        lambda body: body.update(node=''),
        lambda body: body.update(node='x' * 65),
        lambda body: body.pop('generated_at'),
        lambda body: body.update(devices=[{'u': 'u1'}]),
        lambda body: body['devices'][0].update(t='not-a-date'),
        lambda body: body['devices'][0].update(exit=5),
        lambda body: body.update(devices=[body['devices'][0]] * 5001),
    ],
)
def test_report_with_broken_body_is_rejected_and_not_stored(monkeypatch, mutate):
    body = _report('HUB', [_device(10, TAG_DE)])
    mutate(body)
    with _client(monkeypatch) as (http, fake_cache):
        response = http.post('/cabinet/admin/stats/locations/report', json=body, headers=HEADERS)

    assert response.status_code == 422
    assert fake_cache.store == {}


def test_report_returns_503_when_cache_unavailable(monkeypatch):
    with _client(monkeypatch, cache_connected=False) as (http, _cache):
        response = http.post('/cabinet/admin/stats/locations/report', json=_report('HUB', []), headers=HEADERS)

    assert response.status_code == 503


def test_get_merges_reports_from_two_nodes(monkeypatch):
    with _client(monkeypatch) as (http, _cache):
        for body in (
            _report('NL-A', [_device(60, TAG_NL)]),
            _report(
                'HUB',
                [
                    _device(10, TAG_DE, exit_tag='DE1'),
                    _device(10, TAG_AUTO, exit_tag='FI2', user='u2', device='d2'),
                ],
            ),
        ):
            assert http.post('/cabinet/admin/stats/locations/report', json=body, headers=HEADERS).status_code == 200

        response = http.get('/cabinet/admin/stats/locations')

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['enabled'] is True
    assert payload['window_seconds'] == 300
    by_title = {item['title']: item for item in payload['locations']}
    assert by_title['🇩🇪 Germany']['users'] == 1
    assert by_title['🇩🇪 Germany']['exits'] == []
    assert by_title['🇪🇺 Europe (AUTO)']['exits'] == [{'name': 'FI2', 'users': 1}]
    assert by_title['🇩🇪 Germany']['source'] == 'counter'
    assert by_title['🇳🇱 Netherlands']['users'] == 0
    assert by_title['🇳🇱 Netherlands']['node_name'] == 'NL-A'
    assert by_title['🇳🇱 Netherlands']['reported_at'] is not None
    # Ноды без отчёта и с одной карточкой — число из панели.
    assert by_title['🇸🇪 Sweden']['source'] == 'panel'
    assert by_title['🇸🇪 Sweden']['users'] == 8
    # Порядок — по view_position; скрытые и разделители не попадают.
    assert [item['title'] for item in payload['locations']] == [
        '🇪🇺 Europe (AUTO)',
        '🇳🇱 Netherlands',
        '🇩🇪 Germany',
        '🇷🇺 Russia #1',
        '🇷🇺 Russia #2',
        '🇳🇱 Netherlands (ads)',
        '🇸🇪 Sweden',
    ]
    assert '🎬 Streaming — Germany' not in by_title
    assert '👇 Gaming 👇' not in by_title


def test_get_reports_disabled_without_token_and_skips_panel(monkeypatch):
    with _client(monkeypatch, token='', panel_fails=True) as (http, _cache):
        response = http.get('/cabinet/admin/stats/locations')

    assert response.status_code == 200, response.text
    payload = response.json()
    assert (payload['enabled'], payload['locations']) == (False, [])


def test_get_skips_corrupted_stored_report(monkeypatch):
    with _client(monkeypatch) as (http, fake_cache):
        fake_cache.store['location_report:BAD'] = {'node': 'BAD', 'generated_at': 'x', 'devices': [{'u': 1}]}
        response = http.get('/cabinet/admin/stats/locations')

    assert response.status_code == 200, response.text
    assert all(item['source'] != 'counter' for item in response.json()['locations'])


def test_get_returns_503_when_panel_fails(monkeypatch):
    with _client(monkeypatch, panel_fails=True) as (http, _cache):
        response = http.get('/cabinet/admin/stats/locations')

    assert response.status_code == 503
    assert response.json() == {'detail': 'Failed to load locations'}
