"""Весь аккаунт DPI//CHECKER в кабинете: запуски не из кабинета (сайт, их бот, API, прогоны мониторов)
видны в истории и открываются как свои; построчный отчёт — все поля, но без ключей; журнал доставки
вебхуков; монитор с уведомлением в группу и монитор с сайта, получающий адрес вебхука бота."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import settings
from app.database.crud import dpichecker as crud
from app.database.models import Base
from app.services.dpichecker.errors import ActionNotFound
from app.services.dpichecker.service import DpiCheckerService
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture
from tests.fixtures.postgres_db import postgres_session, postgres_sessions


pytestmark = pytest.mark.postgres
TABLES = list(Base.metadata.sorted_tables)


def _fx(name: str):
    return load_dpichecker_fixture(name)['body']


class FakeAPI:
    def __init__(self, *, monitor=None):
        self.calls: list[tuple] = []
        self._monitor = monitor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_checks(self, *, kind='check', check_type=None, limit=25, offset=0):
        self.calls.append(('list_checks', kind, check_type, limit, offset))
        return {'check': _fx('checks_list'), 'probe': _fx('checks_probe'), 'noisy': _fx('checks_noisy')}[kind]

    async def get_check(self, check_id):
        self.calls.append(('get_check', check_id))
        await asyncio.sleep(0)  # отдать ход: два одновременных «Открыть» оба успевают не найти строку
        name = {5286: 'check_vpn', 5313: 'check_mtproto'}.get(check_id)
        if name is None:
            from app.external.dpichecker_api import DpiCheckerAPIError

            raise DpiCheckerAPIError(code='not_found', message='Check not found', status=404)
        return _fx(name)

    async def get_probe(self, scan_id):
        self.calls.append(('get_probe', scan_id))
        return _fx('probe_done')

    async def get_noisy(self, scan_id):
        self.calls.append(('get_noisy', scan_id))
        return _fx('noisy_done')

    async def report_json(self, check_id):
        self.calls.append(('report_json', check_id))
        return _fx('report_vpn') if check_id == 5286 else _fx('report_ip')

    async def webhook_deliveries(self, *, limit=25, offset=0):
        self.calls.append(('deliveries', limit, offset))
        return _fx('webhook_deliveries')

    async def create_monitor(self, body):
        self.calls.append(('create_monitor', body))
        linked = body.get('notify') == 'group'
        return {**_fx('monitor_created'), 'notify': body.get('notify', 'dm'), 'link_code': 'AB12' if linked else None}

    async def get_monitor(self, monitor_id):
        self.calls.append(('get_monitor', monitor_id))
        return {**_fx('monitor_created'), 'id': monitor_id, **(self._monitor or {})}

    async def update_monitor(self, monitor_id, body):
        self.calls.append(('update_monitor', monitor_id, body))
        return {**_fx('monitor_created'), 'id': monitor_id, **body}

    async def list_monitors(self, *, limit=100, offset=0):
        return {'items': [self._monitor]}


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', True)
    monkeypatch.setattr(settings, 'DPICHECKER_API_KEY', 'k')
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example')


async def _nosleep(_):
    return None


def _service(api) -> DpiCheckerService:
    return DpiCheckerService(api_factory=lambda: api, sleep=_nosleep)


# ------------------------------------------------------------------ история аккаунта


async def test_account_checks_mark_own_rows(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        api = FakeAPI()
        service = _service(api)
        own = await service.adopt_remote(db, 'check', 5286, admin_id=None)
        page = await service.account_checks(db, kind='check', check_type='vpn', limit=10, offset=0)
    assert api.calls[-1] == ('list_checks', 'check', 'vpn', 10, 0)
    assert page['total'] == _fx('checks_list')['total']
    first = page['items'][0]
    assert first['id'] == 5286 and first['action_id'] == own.id
    assert first['location'] == 'russia'  # «Россия» у сервиса бывает и так
    assert all(item['action_id'] is None for item in page['items'][1:])


async def test_account_probe_sums_become_numbers(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        page = await _service(FakeAPI()).account_checks(db, kind='probe', check_type=None, limit=25, offset=0)
    item = page['items'][0]
    assert item['usd_cost'] == pytest.approx(1.0331)
    assert item['fixed_cost'] == 1.0 and item['traffic_cost'] == pytest.approx(0.0331)


async def test_adopt_check_names_keys_without_leaking_them(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        action = await _service(FakeAPI()).adopt_remote(db, 'check', 5286, admin_id=None)
    assert (action.kind, action.remote_id, action.check_type) == ('check', 5286, 'vpn')
    assert action.source == 'site' and action.source_ref == 'bot'
    assert action.location == 'russia' and action.status == 'completed'
    assert action.cost_usd == Decimal('0.0600')
    assert action.created_at == datetime(2026, 9, 24, 6, 25, 52, 162000, tzinfo=UTC)
    assert [t['name'] for t in action.targets] == ['🇪🇪 Estonia', '🇩🇪 Germany']  # порядок — как в ответе
    assert '://' not in action.label
    assert action.resource_count == 2


async def test_adopt_mtproto_hides_proxy_link(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        action = await _service(FakeAPI()).adopt_remote(db, 'check', 5313, admin_id=None)
    # Имя прокси — «сервер:порт», как у своих запусков; сама ссылка с секретом наружу не идёт.
    assert [t['name'] for t in action.targets] == ['proxy.example:443']
    assert '://' not in action.label and 'secret' not in action.label


@pytest.mark.parametrize(('kind', 'cost'), [('probe', Decimal('1.0331')), ('noisy', Decimal('0.0000'))])
async def test_adopt_scan(kind, cost, postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        action = await _service(FakeAPI()).adopt_remote(db, kind, 88 if kind == 'probe' else 2628, admin_id=None)
    assert action.kind == kind and action.label == '198.51.100.0/24'
    assert action.status == 'done' and action.cost_usd == cost
    assert action.request == {'target': '198.51.100.0/24'}


async def test_adopt_twice_returns_same_row(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        service = _service(FakeAPI())
        first = await service.adopt_remote(db, 'check', 5286, admin_id=None)
        second = await service.adopt_remote(db, 'check', 5286, admin_id=None)
    assert first.id == second.id


async def test_adopt_concurrently_one_row(postgres_database):
    """Два нажатия «Открыть» одновременно — одна строка, без ошибки второму."""
    async with postgres_sessions(postgres_database, TABLES) as (db_a, db_b):
        service = _service(FakeAPI())
        a, b = await asyncio.gather(
            service.adopt_remote(db_a, 'check', 5286, admin_id=None),
            service.adopt_remote(db_b, 'check', 5286, admin_id=None),
        )
        assert a.id == b.id
        assert len((await crud.list_actions(db_a, kind='check'))[0]) == 1


async def test_adopt_unknown_check_is_not_found(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        with pytest.raises(ActionNotFound):
            await _service(FakeAPI()).adopt_remote(db, 'check', 1, admin_id=None)


# ------------------------------------------------------------------ построчный отчёт


async def test_report_table_hides_vpn_keys(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        service = _service(FakeAPI())
        action = await service.adopt_remote(db, 'check', 5286, admin_id=None)
        table = await service.report_table(db, action.id)
    assert 'uri' not in table['columns'] and table['columns'][0] == 'name'
    assert all('://' not in str(value) for row in table['rows'] for value in row.values())
    assert table['rows'][0]['name'] == '🇫🇮 Finland'
    assert table['rows'][-1]['is_direct'] is True
    assert 'is_direct' not in table['columns']  # признак строки, а не колонка таблицы


async def test_report_table_control_check_is_yes_no(postgres_database):
    """Контрольная проверка точки — «есть ли у точки интернет», а не строка «target: …, accessible: False»."""
    report = {
        'id': 1,
        'check_type': 'vpn',
        'columns': ['uri', 'host', 'control_check'],
        'rows': [
            {'uri': 'vless://masked-1', 'host': 'FI', 'control_check': {'target': 'google.com', 'accessible': False}}
        ],
    }
    from app.services.dpichecker.presenter import present_report

    assert present_report(report, {})['rows'][0]['control_check'] is False


async def test_report_table_keeps_ip_address(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        action = await crud.create_action(
            db,
            kind='check',
            admin_user_id=None,
            check_type='ip',
            location='russia',
            pop_count=1,
            resource_count=1,
            source='paste',
            source_ref=None,
            label='fi',
            targets=[{'value': 'fi.example', 'name': 'Finland'}],
            request={},
        )
        action.remote_id = 5197
        table = await _service(FakeAPI()).report_table(db, action.id)
    assert table['rows'][0]['name'] == 'Finland' and table['rows'][0]['resource'] == 'fi.example'
    assert table['check_type'] == 'ip'


# ------------------------------------------------------------------ журнал доставки


async def test_deliveries_passthrough_page():
    page = await _service(FakeAPI()).webhook_deliveries(limit=5, offset=0)
    assert page['total'] == 7 and page['items'][0]['event'] == 'check.completed'
    assert page['items'][0]['url'] == 'https://bot.example/dpichecker/webhook'


# ------------------------------------------------------------------ мониторы


MONITOR = {
    'check_type': 'ip',
    'location': 'russia',
    'pop_ids': [39],
    'targets': [{'value': 'fi.example', 'name': 'Finland'}],
    'source': 'paste',
    'source_ref': None,
    'label': 'Finland',
    'interval_hours': 6,
    'alert_after_fails': 2,
    'notify_on_success': False,
}


async def test_monitor_notify_group_sent_and_code_shown(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        api = FakeAPI()
        service = _service(api)
        action = await service.create_monitor(db, admin_id=None, notify='group', **MONITOR)
        api._monitor = {**_fx('monitor_created'), 'id': action.remote_id, 'notify': 'group', 'link_code': 'AB12'}
        items = await service.list_monitors(db)
    assert api.calls[0][1]['notify'] == 'group'
    assert items[0]['link_code'] == 'AB12'
    assert 'callback_url' not in items[0] and 'resources' not in items[0]


async def test_monitor_code_hidden_once_group_linked(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        api = FakeAPI(monitor={**_fx('monitor_created'), 'notify': 'group', 'group_linked': True, 'link_code': 'X'})
        items = await _service(api).list_monitors(db)
    assert items[0]['link_code'] is None


async def test_adopted_monitor_gets_bot_webhook(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        api = FakeAPI(monitor={'callback_url': None})
        await _service(api).adopt_monitor(db, 91, admin_id=None)
    assert ('update_monitor', 91, {'callback_url': 'https://bot.example/dpichecker/webhook'}) in api.calls


async def test_adopted_monitor_with_our_webhook_not_patched(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        api = FakeAPI(monitor={'callback_url': 'https://bot.example/dpichecker/webhook'})
        await _service(api).adopt_monitor(db, 92, admin_id=None)
    assert not [call for call in api.calls if call[0] == 'update_monitor']


async def test_deleted_monitor_not_given_webhook(postgres_database):
    """Удалённый у сервиса монитор берут, только чтобы посмотреть прогоны, — адрес ему ни к чему."""
    async with postgres_session(postgres_database, TABLES) as db:
        api = FakeAPI(monitor={'callback_url': None, 'is_active': False, 'paused_reason': 'deleted_via_api'})
        await _service(api).adopt_monitor(db, 93, admin_id=None)
    assert not [call for call in api.calls if call[0] == 'update_monitor']
