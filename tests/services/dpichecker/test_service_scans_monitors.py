"""Фасад DPI//CHECKER: Соседи бесплатны, Зонд — фикс сразу и трафик по итогу (суммы у Зонда строками),
монитор получает адрес вебхука бота (если он есть), список мониторов подписан своими именами,
правка монитора пропускает только известные поля, история — своя запись."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import settings
from app.database.crud import dpichecker as crud
from app.database.models import Base, User
from app.external.dpichecker_api import DpiCheckerAPIError
from app.services.dpichecker.errors import ActionNotFound, LaunchRefused
from app.services.dpichecker.service import DpiCheckerService
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


def _fx(name: str):
    return load_dpichecker_fixture(name)['body']


class FakeAPI:
    def __init__(self, *, monitors=None, noisy_error=None):
        self.calls: list[tuple] = []
        self._monitors = monitors
        self._noisy_error = noisy_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def start_noisy(self, target, *, idempotency_key, callback_url=None):
        self.calls.append(('noisy', target, idempotency_key, callback_url))
        if self._noisy_error:
            raise self._noisy_error
        return _fx('noisy_run')

    async def get_noisy(self, scan_id):
        self.calls.append(('get_noisy', scan_id))
        return _fx('noisy_done')

    async def start_probe(self, target, *, idempotency_key, callback_url=None):
        self.calls.append(('probe', target, idempotency_key, callback_url))
        return _fx('probe_run')

    async def get_probe(self, scan_id):
        self.calls.append(('get_probe', scan_id))
        return _fx('probe_done')

    async def create_monitor(self, body):
        self.calls.append(('create_monitor', body))
        return _fx('monitor_created')

    async def list_monitors(self, *, limit=100, offset=0):
        return self._monitors

    async def update_monitor(self, monitor_id, body):
        self.calls.append(('update_monitor', monitor_id, body))
        return {**_fx('monitor_created'), **body}

    async def delete_monitor(self, monitor_id):
        self.calls.append(('delete_monitor', monitor_id))
        return _fx('monitor_deleted')

    async def monitor_runs(self, monitor_id, *, limit=25, offset=0):
        self.calls.append(('runs', monitor_id, limit, offset))
        return _fx('monitor_runs')

    async def cheremsha(self, resources):
        self.calls.append(('cheremsha', resources))
        return _fx('cheremsha')


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', True)
    monkeypatch.setattr(settings, 'DPICHECKER_API_KEY', 'k')
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example')


async def _nosleep(_):
    return None


def _service(api) -> DpiCheckerService:
    return DpiCheckerService(api_factory=lambda: api, sleep=_nosleep)


async def _admin(db) -> User:
    user = User(telegram_id=781, first_name='a', language='ru', status='active')
    db.add(user)
    await db.flush()
    return user


SCAN = {'target': '198.51.100.0/24', 'source': 'paste', 'source_ref': None, 'label': ''}
MONITOR = {
    'check_type': 'ip',
    'location': 'russia',
    'pop_ids': [39, 35],
    'targets': [{'value': 'fi.example', 'name': 'Finland'}],
    'source': 'panel_hosts',
    'source_ref': 'h1',
    'label': 'Finland',
    'interval_hours': 6,
    'alert_after_fails': 2,
    'notify_on_success': False,
    'probe_mode': 'auto',
}


async def test_noisy_is_free_and_remembers_scan_id(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).launch_noisy(db, admin_id=admin.id, **SCAN)
        assert (action.kind, action.remote_id, action.cost_usd) == ('noisy', 2628, Decimal('0.0000'))
        assert action.label == '198.51.100.0/24'
        assert api.calls[0][2] == action.idempotency_key


async def test_noisy_quota_refusal_is_words(postgres_database):
    api = FakeAPI(noisy_error=DpiCheckerAPIError(code='quota_exceeded', message='x', status=429))
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(LaunchRefused) as info:
            await _service(api).launch_noisy(db, admin_id=admin.id, **SCAN)
        assert info.value.status == 429 and 'Лимит' in info.value.message


async def test_probe_cost_is_fixed_then_traffic_added_when_done(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.launch_probe(db, admin_id=admin.id, **SCAN)
        assert (action.kind, action.remote_id, action.cost_usd) == ('probe', 88, Decimal('1.0000'))
        view = await service.get_scan(db, action.id)
        assert view['action'].status == 'done'
        assert view['action'].cost_usd == Decimal('1.0331')
        assert view['scan']['fixed_cost'] == 1.0 and view['scan']['traffic_cost'] == pytest.approx(0.0331)


async def test_get_scan_of_noisy_reads_noisy(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.launch_noisy(db, admin_id=admin.id, **SCAN)
        view = await service.get_scan(db, action.id)
        assert view['scan']['analysis']['vpn_like']
        assert ('get_noisy', 2628) in api.calls


async def test_get_scan_refuses_check_rows(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        row = await crud.create_action(
            db,
            kind=crud.KIND_CHECK,
            admin_user_id=admin.id,
            check_type='ip',
            location='russia',
            pop_count=1,
            resource_count=1,
            source='paste',
            source_ref=None,
            label='x',
            targets=[],
            request={},
        )
        with pytest.raises(ActionNotFound):
            await _service(FakeAPI()).get_scan(db, row.id)


async def test_monitor_gets_bot_webhook_url(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).create_monitor(db, admin_id=admin.id, **MONITOR)
        body = api.calls[0][1]
        assert body['callback_url'] == 'https://bot.example/dpichecker/webhook'
        assert body['resources'] == ['fi.example'] and body['interval_hours'] == 6
        assert body['probe_mode'] == 'auto'
        assert (action.kind, action.remote_id, action.cost_usd) == ('monitor', 77, None)
        assert action.status == 'active'


async def test_monitor_without_public_url_has_no_callback(monkeypatch, postgres_database):
    monkeypatch.setattr(settings, 'WEBHOOK_URL', None)
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        await _service(api).create_monitor(db, admin_id=admin.id, **MONITOR)
        assert 'callback_url' not in api.calls[0][1]


async def test_vpn_monitor_sends_keys_as_resources(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        vpn = {**MONITOR, 'check_type': 'vpn', 'targets': [{'value': 'vless://a@x.example:443#A', 'name': 'A'}]}
        await _service(api).create_monitor(db, admin_id=admin.id, **vpn)
        body = api.calls[0][1]
        assert body['resources'] == ['vless://a@x.example:443#A'] and 'probe_mode' not in body


async def test_monitor_list_joins_own_labels(postgres_database):
    own = _fx('monitor_created')
    foreign = {**own, 'id': 99}
    api = FakeAPI(monitors={'total': 2, 'items': [own, foreign]})
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.create_monitor(db, admin_id=admin.id, **MONITOR)
        items = await service.list_monitors(db)
        by_id = {item['id']: item for item in items}
        assert by_id[77]['action_id'] == action.id and by_id[77]['label'] == 'Finland'
        assert by_id[99]['action_id'] is None and by_id[99]['label'] is None
        assert 'callback_url' not in by_id[77]


async def test_monitor_patch_only_known_fields(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.create_monitor(db, admin_id=admin.id, **MONITOR)
        await service.update_monitor(db, action.id, {'is_active': False, 'foo': 1, 'callback_url': 'https://evil'})
        assert api.calls[-1] == ('update_monitor', 77, {'is_active': False})


async def test_monitor_delete_marks_row(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.create_monitor(db, admin_id=admin.id, **MONITOR)
        await service.delete_monitor(db, action.id)
        assert action.status == 'deleted'
        assert await crud.list_monitors(db) == []


async def test_monitor_runs_passes_paging(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.create_monitor(db, admin_id=admin.id, **MONITOR)
        runs = await service.monitor_runs(db, action.id, limit=5, offset=10)
        assert runs['items'][0]['check_id'] == 5318
        assert api.calls[-1] == ('runs', 77, 5, 10)


async def test_cheremsha_limits_to_twenty():
    api = FakeAPI()
    await _service(api).cheremsha([f'd{i}.ru' for i in range(25)])
    assert len(api.calls[0][1]) == 20


async def test_history_filters_own_rows(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        await service.launch_noisy(db, admin_id=admin.id, **SCAN)
        items, total = await service.history(db, kind='noisy', check_type=None, admin_user_id=admin.id)
        assert total == 1 and items[0].kind == 'noisy'


async def test_monitor_list_hides_keys(postgres_database):
    own = {**_fx('monitor_created'), 'check_type': 'vpn', 'resources': ['vless://secret@x.example:443']}
    api = FakeAPI(monitors={'total': 1, 'items': [own]})
    async with postgres_session(postgres_database, TABLES) as db:
        items = await _service(api).list_monitors(db)
    assert 'resources' not in items[0] and items[0]['resource_count'] == 1
    assert 'secret' not in str(items)
