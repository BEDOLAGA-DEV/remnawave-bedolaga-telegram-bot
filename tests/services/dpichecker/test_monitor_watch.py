"""Обходчик мониторов DPI//CHECKER: о завершённом прогоне — одно сообщение, тревога и восстановление
по правилам монитора, прогон «в работе» ждёт, удалённый у сервиса монитор помечается, сбой одного
монитора не рвёт обход. Сервис не шлёт событие о завершении прогона — итог забираем сами."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.database.crud import dpichecker as crud
from app.database.models import Base, User
from app.external.dpichecker_api import DpiCheckerAPIError
from app.services.dpichecker.monitor_watch import MonitorWatch, should_notify
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture
from tests.fixtures.postgres_db import postgres_session


TABLES = list(Base.metadata.sorted_tables)
pg = pytest.mark.postgres


def _fx(name: str):
    return load_dpichecker_fixture(name)['body']


def test_running_run_is_not_reported():
    assert should_notify({'notify_on_success': True}, {'check_status': 'running'}, 'up') is False


def test_success_reported_only_when_asked():
    monitor = {'notify_on_success': True, 'alert_after_fails': 2, 'consecutive_fails': 0, 'last_status': 'up'}
    assert should_notify(monitor, {'check_status': 'completed'}, 'up') is True
    assert should_notify({**monitor, 'notify_on_success': False}, {'check_status': 'completed'}, 'up') is False


def test_alert_after_threshold_and_recovery():
    down = {'notify_on_success': False, 'alert_after_fails': 2, 'consecutive_fails': 2, 'last_status': 'down'}
    assert should_notify(down, {'check_status': 'completed'}, 'up') is True
    assert should_notify({**down, 'consecutive_fails': 1}, {'check_status': 'completed'}, 'up') is False
    healed = {**down, 'consecutive_fails': 0, 'last_status': 'up'}
    assert should_notify(healed, {'check_status': 'completed'}, 'down') is True
    assert should_notify(healed, {'check_status': 'completed'}, 'up') is False


class FakeAPI:
    def __init__(self, *, monitor=None, runs=None, check=None, missing=False, broken=False):
        self.monitor, self.runs, self.check, self.missing, self.broken = monitor, runs, check, missing, broken

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_monitor(self, monitor_id):
        if self.broken:
            raise RuntimeError('boom')
        if self.missing:
            raise DpiCheckerAPIError(code='not_found', message='x', status=404)
        return self.monitor

    async def monitor_runs(self, monitor_id, *, limit=25, offset=0):
        return self.runs

    async def get_check(self, check_id):
        return self.check


def _same_session(db):
    @asynccontextmanager
    async def factory():
        yield db

    return factory


async def _never(text):
    raise AssertionError(f'уведомление не ожидалось: {text}')


async def _monitor_row(db, remote_id: int = 77):
    admin = User(telegram_id=779 + remote_id, first_name='m', language='ru', status='active')
    db.add(admin)
    await db.flush()
    row = await crud.create_action(
        db,
        kind=crud.KIND_MONITOR,
        admin_user_id=admin.id,
        check_type='ip',
        location='russia',
        pop_count=10,
        resource_count=1,
        source='panel_hosts',
        source_ref='h1',
        label='Finland',
        targets=[{'value': 'google.com', 'name': 'Finland'}],
        request={},
    )
    row.remote_id, row.status = remote_id, 'active'
    await db.commit()
    return row


@pg
async def test_completed_run_reported_once(postgres_database):
    sent: list[str] = []

    async def notify(text):
        sent.append(text)
        return True

    api = FakeAPI(monitor=_fx('monitor_after_run'), runs=_fx('monitor_runs'), check=_fx('check_watcher_run'))
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _monitor_row(db)
        watch = MonitorWatch(api_factory=lambda: api, session_factory=_same_session(db), notify=notify)
        assert await watch.sweep() == 1
        assert await watch.sweep() == 0
        await db.refresh(row)
        assert row.last_run_id == 18890 and row.status == 'up'
    assert len(sent) == 1
    assert 'Finland' in sent[0] and '10 из 10' in sent[0] and 'google.com' not in sent[0].split('Finland')[0]


@pg
async def test_quiet_run_is_remembered_without_message(postgres_database):
    monitor = {**_fx('monitor_after_run'), 'notify_on_success': False}
    api = FakeAPI(monitor=monitor, runs=_fx('monitor_runs'), check=_fx('check_watcher_run'))
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _monitor_row(db)
        watch = MonitorWatch(api_factory=lambda: api, session_factory=_same_session(db), notify=_never)
        assert await watch.sweep() == 0
        await db.refresh(row)
        assert row.last_run_id == 18890


@pg
async def test_run_in_progress_waits(postgres_database):
    runs = {'items': [{**_fx('monitor_runs')['items'][0], 'check_status': 'active', 'status': 'running'}]}
    api = FakeAPI(monitor=_fx('monitor_after_run'), runs=runs, check=None)
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _monitor_row(db)
        watch = MonitorWatch(api_factory=lambda: api, session_factory=_same_session(db), notify=_never)
        assert await watch.sweep() == 0
        await db.refresh(row)
        assert row.last_run_id is None


@pg
async def test_monitor_gone_at_service_is_marked_deleted(postgres_database):
    api = FakeAPI(missing=True)
    async with postgres_session(postgres_database, TABLES) as db:
        row = await _monitor_row(db)
        watch = MonitorWatch(api_factory=lambda: api, session_factory=_same_session(db), notify=_never)
        await watch.sweep()
        await db.refresh(row)
        assert row.status == 'deleted'
        assert await crud.list_monitors(db) == []


@pg
async def test_one_broken_monitor_does_not_stop_sweep(postgres_database):
    sent: list[str] = []

    async def notify(text):
        sent.append(text)
        return True

    good = FakeAPI(monitor=_fx('monitor_after_run'), runs=_fx('monitor_runs'), check=_fx('check_watcher_run'))
    broken = FakeAPI(broken=True)
    async with postgres_session(postgres_database, TABLES) as db:
        await _monitor_row(db, remote_id=76)
        await _monitor_row(db, remote_id=77)
        apis = iter([broken, good])
        watch = MonitorWatch(api_factory=lambda: next(apis), session_factory=_same_session(db), notify=notify)
        assert await watch.sweep() == 1
    assert len(sent) == 1
