"""Находки ревью ветки DPI//CHECKER (2026-09-24), каждая закреплена тестом:
монитор не создаётся повторно после обрыва, «ещё обрабатывается» — ждать тем же ключом, строка не
зависает в submitting, ключи без имени не становятся «именем», события идут на адрес бота,
незаконченный запуск можно переспросить тем же ключом, выключенный модуль — обходчик молчит."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.config import settings
from app.database.crud import dpichecker as crud
from app.database.models import Base, User
from app.external.dpichecker_api import DpiCheckerAPIError, DpiCheckerGatewayError
from app.services.dpichecker.errors import LaunchRefused
from app.services.dpichecker.presenter import present_check
from app.services.dpichecker.service import DpiCheckerService
from app.services.dpichecker.targets import safe_name
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture
from tests.fixtures.postgres_db import postgres_session


TABLES = list(Base.metadata.sorted_tables)
pg = pytest.mark.postgres
KEY = 'vless://11111111-2222-3333-4444-555555555555@fi.example:443?security=reality&pbk=x'
MTPROTO = 'tg://proxy?server=proxy.example&port=8443&secret=ee0011223344'


class FakeAPI:
    def __init__(self, *, errors=(), response=None, cancel_on_start=False):
        self.calls: list[tuple] = []
        self._errors = list(errors)
        self._response = response or {'check_id': 5309, 'status': 'pending', 'estimated_cost': 0.04}
        self._cancel = cancel_on_start

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def _answer(self, name, *args):
        self.calls.append((name, *args))
        if self._cancel:
            raise asyncio.CancelledError
        if self._errors:
            raise self._errors.pop(0)
        return self._response

    async def start_check(self, check_type, body, *, idempotency_key):
        return await self._answer('start', check_type, body, idempotency_key)

    async def create_monitor(self, body):
        return await self._answer('create_monitor', body)

    async def start_noisy(self, target, *, idempotency_key, callback_url=None):
        return await self._answer('noisy', target, idempotency_key, callback_url)

    async def get_monitor(self, monitor_id):
        raise AssertionError('обходчик выключенного модуля не должен звать сервис')


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
    user = User(telegram_id=951, first_name='r', language='ru', status='active')
    db.add(user)
    await db.flush()
    return user


def _launch(**extra):
    base = {
        'check_type': 'ip',
        'location': 'russia',
        'pop_ids': [39],
        'targets': [{'value': 'google.com', 'name': 'google.com'}],
        'source': 'paste',
        'source_ref': None,
        'label': '',
        'probe_mode': 'auto',
    }
    return {**base, **extra}


MONITOR = {
    **_launch(),
    'interval_hours': 6,
    'alert_after_fails': 2,
    'notify_on_success': False,
}


# ---------------------------------------------------------------- C1: монитор без ключа идемпотентности


@pg
async def test_monitor_not_recreated_after_gateway_error(postgres_database):
    api = FakeAPI(errors=[DpiCheckerGatewayError(code='timeout', message='t')], response={'id': 77, 'is_active': True})
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(DpiCheckerGatewayError):
            await _service(api).create_monitor(db, admin_id=admin.id, **MONITOR)
        assert [c[0] for c in api.calls] == ['create_monitor']
        rows, _ = await crud.list_actions(db, kind=crud.KIND_MONITOR)
        assert rows[0].status == 'unknown'


# ---------------------------------------------------------------- I1: ещё обрабатывается — ждать тем же ключом


@pg
async def test_in_flight_waits_and_repeats_with_same_key(postgres_database):
    busy = DpiCheckerAPIError(code='idempotency_in_flight', message='busy', status=409)
    api = FakeAPI(errors=[DpiCheckerGatewayError(code='timeout', message='t'), busy])
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).launch_check(db, admin_id=admin.id, **_launch())
        keys = {c[3] for c in api.calls if c[0] == 'start'}
        assert len([c for c in api.calls if c[0] == 'start']) == 3 and keys == {action.idempotency_key}
        assert action.status == 'pending' and action.remote_id == 5309


@pg
async def test_in_flight_forever_is_unknown_not_rejected(postgres_database):
    busy = [DpiCheckerAPIError(code='idempotency_in_flight', message='busy', status=409) for _ in range(10)]
    api = FakeAPI(errors=busy)
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(DpiCheckerGatewayError):
            await _service(api).launch_check(db, admin_id=admin.id, **_launch())
        rows, _ = await crud.list_actions(db)
        assert rows[0].status == 'unknown'


# ---------------------------------------------------------------- I2: строка не зависает в submitting


@pg
async def test_cancelled_request_leaves_row_unknown(postgres_database):
    api = FakeAPI(cancel_on_start=True)
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(asyncio.CancelledError):
            await _service(api).launch_check(db, admin_id=admin.id, **_launch())
        rows, _ = await crud.list_actions(db)
        assert rows[0].status == 'unknown'


@pg
async def test_broken_answer_leaves_row_unknown(postgres_database):
    api = FakeAPI(response={'status': 'pending'})  # без check_id
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(DpiCheckerAPIError):
            await _service(api).launch_check(db, admin_id=admin.id, **_launch())
        rows, _ = await crud.list_actions(db)
        assert rows[0].status == 'unknown'


@pg
async def test_unknown_launch_resubmitted_with_stored_key_and_body(postgres_database):
    silence = [DpiCheckerGatewayError(code='timeout', message='t') for _ in range(3)]
    api = FakeAPI(errors=silence)
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        with pytest.raises(DpiCheckerGatewayError):
            await service.launch_check(db, admin_id=admin.id, **_launch())
        rows, _ = await crud.list_actions(db)
        action = await service.resubmit(db, rows[0].id)
        last = api.calls[-1]
        assert last[3] == action.idempotency_key and last[2] == action.request
        assert (action.status, action.remote_id, action.cost_usd) == ('pending', 5309, Decimal('0.0400'))


@pg
async def test_finished_launch_cannot_be_resubmitted(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.launch_check(db, admin_id=admin.id, **_launch())
        with pytest.raises(LaunchRefused):
            await service.resubmit(db, action.id)


# ---------------------------------------------------------------- I3: ключи не становятся «именем»


def test_safe_name_never_returns_secret():
    assert safe_name('vpn', KEY, '') == 'fi.example:443'
    assert safe_name('vpn', KEY + '#Finland', '') == 'Finland'
    assert safe_name('vpn', KEY, KEY) == 'fi.example:443'
    assert safe_name('mtproto', MTPROTO, '') == 'proxy.example:8443'
    assert 'secret' not in safe_name('mtproto', MTPROTO, MTPROTO)
    assert safe_name('ip', 'google.com', '') == 'google.com'
    assert safe_name('vpn', KEY, 'Мой сервер') == 'Мой сервер'


@pg
async def test_unnamed_keys_stored_with_safe_names(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).launch_check(
            db, admin_id=admin.id, **_launch(check_type='vpn', targets=[{'value': KEY, 'name': ''}])
        )
        assert [t['name'] for t in action.targets] == ['fi.example:443']
        assert 'vless://' not in action.label


@pg
async def test_vpn_rejects_subscription_link_instead_of_keys(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(ValueError, match='подписк'):
            await _service(api).launch_check(
                db,
                admin_id=admin.id,
                **_launch(check_type='vpn', targets=[{'value': 'https://sub.example/abc', 'name': ''}]),
            )
    assert api.calls == []


def test_presenter_does_not_show_key_as_name():
    check = load_dpichecker_fixture('check_vpn')['body']
    uri = check['results'][0]['uri']
    view = present_check(check, {uri: uri})
    assert view['resources'][0]['name'] == check['results'][0]['host']


# ---------------------------------------------------------------- I6: события идут на адрес бота


@pg
async def test_launch_and_scan_carry_callback_url(postgres_database):
    api = FakeAPI(response={'check_id': 1, 'scan_id': 2, 'status': 'pending', 'estimated_cost': 0.01})
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        await service.launch_check(db, admin_id=admin.id, **_launch())
        await service.launch_noisy(
            db, admin_id=admin.id, target='198.51.100.0/24', source='paste', source_ref=None, label=''
        )
    assert api.calls[0][2]['callback_url'] == 'https://bot.example/dpichecker/webhook'
    assert api.calls[1][3] == 'https://bot.example/dpichecker/webhook'


# ---------------------------------------------------------------- I4: выключен на ходу — обходчик молчит


@pg
async def test_sweep_silent_when_module_disabled(monkeypatch, postgres_database):
    from contextlib import asynccontextmanager

    from app.services.dpichecker.monitor_watch import MonitorWatch

    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        row = await crud.create_action(
            db,
            kind=crud.KIND_MONITOR,
            admin_user_id=admin.id,
            check_type='ip',
            location='russia',
            pop_count=1,
            resource_count=1,
            source='paste',
            source_ref=None,
            label='m',
            targets=[],
            request={},
        )
        row.remote_id = 77
        await db.commit()

        @asynccontextmanager
        async def same():
            yield db

        monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', False)
        watch = MonitorWatch(api_factory=FakeAPI, session_factory=same, notify=None)
        assert await watch.sweep() == 0


async def test_background_follows_live_settings(monkeypatch):
    service = DpiCheckerService(api_factory=FakeAPI)

    async def notify(text):
        return True

    service.sync_background(notify)
    assert service.background_running
    monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', False)
    service.sync_background(notify)
    await asyncio.sleep(0)
    assert not service.background_running
    await service.stop_background()


# ---------------------------------------------------------------- I5: подпись: перечитывать секрет не чаще раза в минуту


async def test_secret_refresh_throttled(monkeypatch):
    from app.services.dpichecker import service as service_module

    clock = [1000.0]
    monkeypatch.setattr(service_module.time, 'monotonic', lambda: clock[0])
    calls: list[int] = []

    class SecretAPI(FakeAPI):
        async def webhook_secret(self):
            calls.append(1)
            return 'whsec_test'

    service = DpiCheckerService(api_factory=SecretAPI, sleep=_nosleep)
    await service.webhook_secret()
    for _ in range(20):  # поток мусорных подписей в ту же минуту — сервис больше не спрашиваем
        await service.webhook_secret(refresh=True)
    assert len(calls) == 1
    clock[0] += 61
    await service.webhook_secret(refresh=True)
    await service.webhook_secret(refresh=True)
    assert len(calls) == 2


# ---------------------------------------------------------------- M5: длинный статус сервиса не роняет запись


@pg
async def test_long_service_status_is_trimmed(postgres_database):
    api = FakeAPI(response={'check_id': 9, 'status': 'waiting_for_worker_capacity', 'estimated_cost': 0.01})
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).launch_check(db, admin_id=admin.id, **_launch())
        await db.refresh(action)
        assert len(action.status) <= 16
