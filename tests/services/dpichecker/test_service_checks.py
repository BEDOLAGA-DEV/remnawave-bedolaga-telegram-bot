"""Фасад DPI//CHECKER: запуск пишет строку до сервиса, повтор после обрыва тем же ключом,
отказ сервиса — строка rejected словами, VPN уходит ключами, отмена ставит возврат,
выключенный модуль не трогает сервис, статус объясняет неверный ключ словами."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import settings
from app.database.crud import dpichecker as crud
from app.database.models import Base, User
from app.external.dpichecker_api import DpiCheckerAPIError, DpiCheckerGatewayError
from app.services.dpichecker.errors import ActionNotFound, DpiCheckerDisabled, LaunchRefused, human_error
from app.services.dpichecker.service import DpiCheckerService
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture
from tests.fixtures.postgres_db import postgres_session


TABLES = list(Base.metadata.sorted_tables)
pg = pytest.mark.postgres


class FakeAPI:
    def __init__(self, *, start_errors=(), check=None, profile_error=None):
        self.calls: list[tuple] = []
        self._start_errors = list(start_errors)
        self._check = check
        self._profile_error = profile_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def start_check(self, check_type, body, *, idempotency_key):
        self.calls.append(('start', check_type, body, idempotency_key))
        if self._start_errors:
            raise self._start_errors.pop(0)
        return {'check_id': 5309, 'status': 'pending', 'estimated_cost': 0.04, 'balance': 54.6}

    async def estimate(self, body):
        self.calls.append(('estimate', body))
        return {'estimated_cost': 0.12, 'pops': len(body['pop_ids'])}

    async def get_check(self, check_id):
        self.calls.append(('get', check_id))
        return self._check

    async def wait_check(self, check_id, timeout=60):
        self.calls.append(('wait', check_id, timeout))
        return self._check

    async def cancel_check(self, check_id):
        self.calls.append(('cancel', check_id))
        return {'check_id': check_id, 'status': 'cancelled', 'refunded': 0.04, 'balance': 54.6}

    async def profile(self):
        self.calls.append(('profile',))
        if self._profile_error:
            raise self._profile_error
        return load_dpichecker_fixture('profile')['body']

    async def quota(self):
        self.calls.append(('quota',))
        return load_dpichecker_fixture('quota')['body']


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', True)
    monkeypatch.setattr(settings, 'DPICHECKER_API_KEY', 'k')


async def _nosleep(_seconds):
    return None


def _service(api) -> DpiCheckerService:
    return DpiCheckerService(api_factory=lambda: api, sleep=_nosleep)


async def _admin(db) -> User:
    user = User(telegram_id=778, first_name='a', language='ru', status='active')
    db.add(user)
    await db.flush()
    return user


LAUNCH = {
    'check_type': 'ip',
    'location': 'russia',
    'pop_ids': [39, 35],
    'targets': [{'value': 'google.com', 'name': 'google.com'}],
    'source': 'paste',
    'source_ref': None,
    'label': 'google.com',
    'probe_mode': 'server',
}


@pg
async def test_launch_writes_row_and_remote_id(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).launch_check(db, admin_id=admin.id, **LAUNCH)
        assert (action.remote_id, action.status, action.cost_usd) == (5309, 'pending', Decimal('0.0400'))
        _, check_type, body, key = api.calls[0]
        assert check_type == 'ip'
        assert body == {'location': 'russia', 'pop_ids': [39, 35], 'resources': ['google.com'], 'probe_mode': 'server'}
        assert key == action.idempotency_key
        assert action.request == body


@pg
async def test_launch_retries_same_key_after_gateway_error(postgres_database):
    api = FakeAPI(start_errors=[DpiCheckerGatewayError(code='timeout', message='t')])
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).launch_check(db, admin_id=admin.id, **LAUNCH)
        keys = [call[3] for call in api.calls if call[0] == 'start']
        assert keys == [action.idempotency_key, action.idempotency_key]
        assert action.status == 'pending'


@pg
async def test_gateway_silence_leaves_row_unknown_not_submitting(postgres_database):
    silence = [DpiCheckerGatewayError(code='timeout', message='t') for _ in range(3)]
    api = FakeAPI(start_errors=silence)
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(DpiCheckerGatewayError):
            await _service(api).launch_check(db, admin_id=admin.id, **LAUNCH)
        rows, _ = await crud.list_actions(db)
        assert (rows[0].status, rows[0].error_code) == ('unknown', 'timeout')


@pg
async def test_rate_limit_waits_once_and_repeats(postgres_database):
    api = FakeAPI(start_errors=[DpiCheckerAPIError(code='rate_limited', message='slow', status=429, retry_after=2)])
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        action = await _service(api).launch_check(db, admin_id=admin.id, **LAUNCH)
        assert action.status == 'pending'
        assert len([c for c in api.calls if c[0] == 'start']) == 2


@pg
async def test_refusal_marks_row_rejected(postgres_database):
    api = FakeAPI(start_errors=[DpiCheckerAPIError(code='insufficient_balance', message='no', status=402)])
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        with pytest.raises(LaunchRefused) as info:
            await _service(api).launch_check(db, admin_id=admin.id, **LAUNCH)
        assert (info.value.code, info.value.status) == ('insufficient_balance', 402)
        assert 'Пополн' in info.value.message
        rows, _ = await crud.list_actions(db)
        assert (rows[0].status, rows[0].error_code) == ('rejected', 'insufficient_balance')


@pg
async def test_vpn_launch_sends_keys_not_resources(postgres_database):
    api = FakeAPI()
    keys = [{'value': 'vless://a@x.example:443#A', 'name': 'A'}, {'value': 'vless://b@y.example:443#B', 'name': 'B'}]
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        await _service(api).launch_check(
            db, admin_id=admin.id, **{**LAUNCH, 'check_type': 'vpn', 'targets': keys, 'probe_mode': 'auto'}
        )
    body = api.calls[0][2]
    assert body == {'location': 'russia', 'pop_ids': [39, 35], 'keys': [k['value'] for k in keys]}


async def test_estimate_counts_vpn_keys():
    api = FakeAPI()
    await _service(api).estimate('vpn', 'russia', [39], ['vless://a@x.example:443#A', 'vless://b@y.example:443#B'])
    body = api.calls[0][1]
    assert body['keys'] == ['vless://a@x.example:443#A', 'vless://b@y.example:443#B']
    assert 'subscription_url' not in body and 'resources' not in body


async def test_estimate_ip_uses_resources():
    api = FakeAPI()
    await _service(api).estimate('ip', 'russia', [39], ['google.com'])
    assert api.calls[0][1] == {'check_type': 'ip', 'location': 'russia', 'pop_ids': [39], 'resources': ['google.com']}


@pg
async def test_disabled_module_refuses_before_any_call(monkeypatch, postgres_database):
    monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', False)
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        with pytest.raises(DpiCheckerDisabled):
            await _service(api).launch_check(db, admin_id=None, **LAUNCH)
    assert api.calls == []


async def test_missing_key_refuses_with_words(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_API_KEY', None)
    with pytest.raises(DpiCheckerDisabled) as info:
        await _service(FakeAPI()).estimate('ip', 'russia', [1], ['google.com'])
    assert 'ключ' in info.value.reason


async def test_status_disabled_does_not_call_service(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_ENABLED', False)
    api = FakeAPI()
    status = await _service(api).status()
    assert (status['enabled'], status['configured']) == (False, True)
    assert api.calls == []


async def test_status_reads_balance_and_quota():
    status = await _service(FakeAPI()).status()
    assert status['balance'] == pytest.approx(54.7283)
    assert status['noisy']['limit'] == 10 and status['monitors']['limit'] == 50
    assert status['error'] is None


async def test_status_explains_bad_key_in_words():
    api = FakeAPI(profile_error=DpiCheckerAPIError(code='invalid_api_key', message='bad', status=401))
    status = await _service(api).status()
    assert status['balance'] is None
    assert 'ключ' in status['error'].lower()


@pg
async def test_get_check_presents_and_names_by_targets(postgres_database):
    check = load_dpichecker_fixture('check_ip_server')['body']
    api = FakeAPI(check=check)
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.launch_check(
            db, admin_id=admin.id, **{**LAUNCH, 'targets': [{'value': 'google.com', 'name': 'Гугл'}]}
        )
        waited = await service.get_check(db, action.id, wait=30)  # в очереди — ждём у сервиса
        assert ('wait', 5309, 30) in api.calls
        assert waited['check']['resources'][0]['name'] == 'Гугл'
        assert waited['action'].status == check['status'] == 'completed'
        await service.get_check(db, action.id, wait=30)  # готово — ждать нечего
        assert api.calls[-1] == ('get', 5309)


@pg
async def test_get_check_without_remote_id_shows_row_status(postgres_database):
    api = FakeAPI(start_errors=[DpiCheckerAPIError(code='insufficient_balance', message='no', status=402)])
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        with pytest.raises(LaunchRefused):
            await service.launch_check(db, admin_id=admin.id, **LAUNCH)
        rows, _ = await crud.list_actions(db)
        view = await service.get_check(db, rows[0].id)
        assert view['check']['status'] == 'rejected' and view['check']['resources'] == []
        assert not [c for c in api.calls if c[0] in ('get', 'wait')]


@pg
async def test_unknown_action_is_not_found(postgres_database):
    async with postgres_session(postgres_database, TABLES) as db:
        with pytest.raises(ActionNotFound):
            await _service(FakeAPI()).get_check(db, 999)


@pg
async def test_cancel_records_refund(postgres_database):
    api = FakeAPI()
    async with postgres_session(postgres_database, TABLES) as db:
        admin = await _admin(db)
        service = _service(api)
        action = await service.launch_check(db, admin_id=admin.id, **LAUNCH)
        action = await service.cancel_check(db, action.id)
        assert (action.status, action.refunded_usd) == ('cancelled', Decimal('0.0400'))


def test_human_error_by_code_not_text():
    exc = DpiCheckerAPIError(code='ip_not_allowed', message='whatever text', status=403)
    assert 'IP' in human_error(exc)
    unknown = DpiCheckerAPIError(code='brand_new_code', message='Что-то новое', status=400)
    assert 'Что-то новое' in human_error(unknown)


# ---------------------------------------------------------------- подписка по умолчанию (как у BSCHEKER)


def _panel_service(monkeypatch, links: list[str]) -> DpiCheckerService:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.services.dpichecker import targets

    async def fetch(api, short_uuid, prefer_public=False):
        return list(links) if short_uuid == 'ref-1' else []

    monkeypatch.setattr(targets, 'fetch_panel_links', fetch)

    @asynccontextmanager
    async def panel():
        yield SimpleNamespace()

    return DpiCheckerService(api_factory=lambda: FakeAPI(), panel_client=panel, sleep=_nosleep)


async def test_subscription_without_user_takes_default_from_settings(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_REFERENCE_SUBSCRIPTION', 'ref-1')
    service = _panel_service(monkeypatch, ['vless://u@fi.example:443#Finland', 'trojan://p@de.example:8443#DE'])
    keys = await service.panel_targets(None, kind='subscription', user_id=None)
    assert [(k.name, k.ref) for k in keys] == [('Finland', 'ref-1'), ('DE', 'ref-1')]


async def test_subscription_without_user_and_default_is_explained(monkeypatch):
    from app.services.dpichecker.targets import PanelTargetError

    monkeypatch.setattr(settings, 'DPICHECKER_REFERENCE_SUBSCRIPTION', None)
    service = _panel_service(monkeypatch, [])
    with pytest.raises(PanelTargetError) as info:
        await service.panel_targets(None, kind='subscription', user_id=None)
    assert 'по умолчанию' in str(info.value)


async def test_status_tells_default_subscription_and_its_keys(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_REFERENCE_SUBSCRIPTION', 'ref-1')
    service = _panel_service(monkeypatch, ['vless://u@fi.example:443#Finland'])
    assert (await service.status())['reference'] == {'short_uuid': 'ref-1', 'configs': 1, 'error': None}


async def test_status_without_default_subscription_says_so(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_REFERENCE_SUBSCRIPTION', None)
    reference = (await _panel_service(monkeypatch, []).status())['reference']
    assert reference['short_uuid'] is None and reference['configs'] == 0 and reference['error']


async def test_status_survives_broken_default_subscription(monkeypatch):
    monkeypatch.setattr(settings, 'DPICHECKER_REFERENCE_SUBSCRIPTION', 'gone')
    status = await _panel_service(monkeypatch, []).status()
    assert status['balance'] is not None
    assert status['reference']['short_uuid'] == 'gone' and status['reference']['error']


async def test_default_subscription_may_be_a_link_expanded_by_the_service(monkeypatch):
    """Владелец вставляет ссылку подписки — её разворачивает сам DPI//CHECKER, панель не нужна."""
    link = 'https://sub.example/Ab12Cd34Ef56Gh78'
    monkeypatch.setattr(settings, 'DPICHECKER_REFERENCE_SUBSCRIPTION', f'  {link}  ')

    class ParseAPI(FakeAPI):
        async def parse(self, check_type, text):
            self.calls.append(('parse', check_type, text))
            return load_dpichecker_fixture('parse_vpn')['body']

    api = ParseAPI()

    def panel():
        raise AssertionError('ссылку не надо искать в панели')

    service = DpiCheckerService(api_factory=lambda: api, panel_client=panel, sleep=_nosleep)
    keys = await service.panel_targets(None, kind='subscription', user_id=None)
    assert ('parse', 'vpn', link) in api.calls
    assert [(key.name, key.value.split('://')[0]) for key in keys] == [
        ('test', 'vless'),
        ('ss1', 'ss'),
        ('hy', 'hysteria2'),
    ]
    assert {key.ref for key in keys} == {'Ab12Cd34Ef56Gh78'}
    assert (await service.status())['reference'] == {
        'short_uuid': 'Ab12Cd34Ef56Gh78',
        'configs': len(keys),
        'error': None,
    }
