"""Фасад: выключено → ReachabilityDisabled; preview считает симки, пропуски и цену до денег;
create_job проверяет занятость и потолок, пишет задачу и запускает фон."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect as sa_inspect

from app.database.crud import reachability as crud
from app.database.models import User
from app.external.bschek_api import BschekAPIError
from app.external.remnawave_api import RemnaWaveHost
from app.services.reachability.gate import PaidCallGate
from app.services.reachability.jobs import JobNotCancellable, JobRunner, RunnerConfig
from app.services.reachability.pricing import CostLimitExceeded
from app.services.reachability.resolver import TargetResolutionError
from app.services.reachability.service import (
    JobNotFound,
    PanelUnavailable,
    ReachabilityBusy,
    ReachabilityDisabled,
    ReachabilityService,
    ReachabilityUnhealthy,
)
from app.services.reachability.subscriptions import SubscriptionFetchError
from app.services.reachability.units import SelectorError
from tests.fixtures.bschek_fixtures import load_bschek_fixture
from tests.services.reachability.fakes import FakeAPI, FakeClock


pytestmark = pytest.mark.asyncio

BS_LINK = (
    'vless://00000000-0000-4000-8000-000000000001@bs-host.example:9443?security=reality&sni=whitelisted.example#BS'
)
HOSTS = [RemnaWaveHost(uuid='h-bs', remark='RU | БС', address='bs-host.example', port=9443, sni='whitelisted.example')]


class FakePanel:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken

    def get_api_client(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                if outer.broken:
                    raise RuntimeError('panel down')
                return outer

            async def __aexit__(self, *exc):
                return None

        return _Ctx()

    async def get_all_hosts(self):
        return HOSTS

    async def get_all_nodes(self):
        return []

    async def get_subscription_info(self, short_uuid):
        if short_uuid not in ('ref-1', 'sub-1'):
            raise RuntimeError('404 User not found')
        return SimpleNamespace(links=[BS_LINK])


class FakeClient(FakeAPI):
    def __init__(self, script=None, *, account_error: Exception | None = None) -> None:
        super().__init__(script)
        self.account_error = account_error
        self.operators_calls = 0
        self.account_calls = 0

    async def get_operators(self, **kwargs):
        self.operators_calls += 1
        return load_bschek_fixture('operators')['body']

    async def get_account(self):
        self.account_calls += 1
        if self.account_error is not None:
            raise self.account_error
        return {k: v for k, v in load_bschek_fixture('account')['body'].items() if k != 'webhook_secret'}

    async def preview_probe(self, body):
        return load_bschek_fixture('pv_bare_mts')['body']

    async def preview_scan(self, body):
        return load_bschek_fixture('sv_one_unit')['body']


def make_service(
    session_factory,
    *,
    enabled: bool = True,
    key: str | None = 'bsk_live_test',
    limit: int = 0,
    reference: str | None = 'ref-1',
    client: FakeClient | None = None,
    panel: FakePanel | None = None,
    url_links: dict[str, list[str] | Exception] | None = None,
) -> ReachabilityService:
    settings_obj = SimpleNamespace(
        BSCHEK_ENABLED=enabled,
        BSCHEK_API_KEY=key,
        BSCHEK_REQUEST_TIMEOUT=200,
        BSCHEK_REFERENCE_SUBSCRIPTION=reference,
        BSCHEK_JOB_COST_LIMIT_KOPEKS=limit,
        is_bschek_enabled=lambda: enabled,
        is_bschek_configured=lambda: bool(key),
        get_bschek_api_url=lambda: 'https://bsbord.com/v1',
    )
    clock = FakeClock()
    api = client or FakeClient()
    runner = JobRunner(
        client_factory=lambda: api,
        gate=PaidCallGate(min_interval=0, clock=clock, sleep=clock.sleep),
        session_factory=session_factory,
        cost_limit_kopeks=lambda: limit,
        config=RunnerConfig(),
        sleep=clock.sleep,
        clock=clock,
    )
    panel_obj = panel or FakePanel()

    async def url_fetcher(url: str) -> list[str]:
        answer = (url_links or {}).get(url)
        if answer is None:
            raise SubscriptionFetchError(f'Не удалось загрузить {url}')
        if isinstance(answer, Exception):
            raise answer
        return list(answer)

    service = ReachabilityService(
        settings_obj=settings_obj,
        session_factory=session_factory,
        remnawave_factory=lambda: panel_obj,
        runner=runner,
        clock=clock,
        url_fetcher=url_fetcher,
    )
    service._client_factory = lambda: api
    return service


async def _admin(db) -> User:
    user = User(telegram_id=1, username='admin', first_name='A', language='ru')
    db.add(user)
    await db.flush()
    return user


PROBE_PAYLOAD = {
    'kind': 'probe',
    'targets': [{'kind': 'host', 'ref': 'h-bs'}],
    'units': ['mts'],
    'dpi': 'on',
    'probes': {'tcp': True, 'sni': True},
}
VLESS_PAYLOAD = {
    'kind': 'vless',
    'targets': [{'kind': 'subscription_config', 'short_uuid': 'ref-1', 'index': 0}],
    'units': ['*|цфо|on'],
    'dpi': 'on',
    'probes': {},
    'core': '',
}


# ---------------------------------------------------------------- доступ и статус


async def test_disabled_integration_raises(session_factory) -> None:
    service = make_service(session_factory, enabled=False)
    async with session_factory() as db:
        with pytest.raises(ReachabilityDisabled):
            await service.preview(db, PROBE_PAYLOAD)
        with pytest.raises(ReachabilityDisabled):
            await service.units()
        status = await service.status(db)
    assert (status['enabled'], status['configured'], status['balance_kopeks'], status['reference']) == (
        False,
        True,
        None,
        None,
    )


async def test_missing_key_is_reported_as_not_configured(session_factory) -> None:
    service = make_service(session_factory, key=None)
    async with session_factory() as db:
        with pytest.raises(ReachabilityDisabled, match='BSCHEK_API_KEY'):
            await service.preview(db, PROBE_PAYLOAD)


async def test_status_reports_balance_without_secret_and_reference(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        status = await service.status(db)
    assert status['enabled'] and status['configured'] and status['healthy']
    assert status['balance_kopeks'] == 100018 and 'webhook_secret' not in str(status)
    assert (status['tier'], status['active_jobs'], status['cost_limit_kopeks']) == ('gold', [], 0)
    assert status['reference'] == {'short_uuid': 'ref-1', 'configs': 1, 'rejected': 0, 'error': None}
    # Номера ядер Xray для фронта: оригинал bsbord.com показывает версии цифрами, а не «stable/prerelease».
    assert set(status['cores']) == {'stable', 'prerelease'}
    assert all(re.fullmatch(r'\d+\.\d+\.\d+', version) for version in status['cores'].values())


async def test_status_lists_active_jobs_and_missing_reference(session_factory) -> None:
    service = make_service(session_factory, reference=None)
    async with session_factory() as db:
        admin = await _admin(db)
        await crud.create_job(
            db,
            kind='scan',
            status='running',
            trigger='manual',
            started_by_user_id=admin.id,
            idempotency_key='k',
            request={},
            targets=[],
            dpi='on',
            phase='polling',
        )
        await db.commit()
        status = await service.status(db)
    assert [(j['kind'], j['phase'], j['started_by_user_id']) for j in status['active_jobs']] == [('scan', 'polling', 1)]
    assert status['reference']['short_uuid'] is None and status['reference']['error']


async def test_auth_error_marks_integration_unhealthy_for_a_while(session_factory) -> None:
    client = FakeClient(account_error=BschekAPIError(code='tier_too_low', message='Нужен тариф выше', status=403))
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        status = await service.status(db)
        assert (status['healthy'], status['health_message']) == (False, 'Нужен тариф выше')
        with pytest.raises(ReachabilityUnhealthy) as excinfo:
            await service.preview(db, PROBE_PAYLOAD)
        assert excinfo.value.until > datetime.now(UTC)
        assert client.account_calls == 1  # пока нездорово — к API не ходим


async def test_account_is_cached_between_calls(session_factory) -> None:
    client = FakeClient()
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        await service.status(db)
        await service.preview(db, PROBE_PAYLOAD)
    assert client.account_calls == 1


# ---------------------------------------------------------------- симки и источники


async def test_units_filters_locally_over_cached_catalog(session_factory) -> None:
    client = FakeClient()
    service = make_service(session_factory, client=client)
    mts_on = await service.units(dpi='on', operator=['MTS'])
    assert [u.op_key for u in mts_on] == ['mts|пфо|on']
    cfo = await service.units(region=['cfo'])
    assert len(cfo) == 6 and client.operators_calls == 1


async def test_hosts_nodes_and_configs_go_through_panel(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        hosts = await service.hosts(db)
        nodes = await service.nodes(db)
        configs = await service.subscription_configs(db)
    assert [h.host.uuid for h in hosts] == ['h-bs'] and hosts[0].target.purpose == 'bs'
    assert nodes == [] and [c.label for c in configs.configs] == ['BS']


async def test_panel_failure_becomes_panel_unavailable(session_factory) -> None:
    service = make_service(session_factory, panel=FakePanel(broken=True))
    async with session_factory() as db:
        with pytest.raises(PanelUnavailable):
            await service.hosts(db)
        with pytest.raises(PanelUnavailable):
            await service.preview(db, PROBE_PAYLOAD)


async def test_subscription_configs_for_user_without_subscription_explains(session_factory) -> None:
    """Пользователь без подписки панели — своя ошибка, а не жалоба на эталон из настроек."""
    service = make_service(session_factory)
    async with session_factory() as db:
        admin = await _admin(db)
        with pytest.raises(TargetResolutionError, match=f'#{admin.id} нет подписки'):
            await service.subscription_configs(db, user_id=admin.id)


async def test_subscription_configs_without_reference_raise(session_factory) -> None:
    service = make_service(session_factory, reference=None)
    async with session_factory() as db:
        with pytest.raises(ReachabilityDisabled, match='BSCHEK_REFERENCE_SUBSCRIPTION'):
            await service.subscription_configs(db)


# ---------------------------------------------------------------- preview


async def test_preview_probe_expands_units_reports_skipped_and_exact_price(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        preview = await service.preview(db, PROBE_PAYLOAD)
    assert preview.units_resolved == ['mts|пфо|on']
    assert [u['op_key'] for u in preview.skipped['dpi_off']] == ['mts|цфо|off', 'mts|дфо|off']
    assert (preview.cost_kopeks, preview.estimate_is_exact, preview.balance_kopeks) == (18, True, 100018)
    assert preview.request['sni_hosts'] == ['whitelisted.example']
    assert preview.warnings == []


async def test_preview_probe_warns_about_bs_host_without_sni_probe(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        preview = await service.preview(db, {**PROBE_PAYLOAD, 'probes': {'tcp': True}})
    assert any('SNI' in warning for warning in preview.warnings)


async def test_preview_unknown_selector_is_rejected_before_api(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        with pytest.raises(SelectorError, match='nokia'):
            await service.preview(db, {**PROBE_PAYLOAD, 'units': ['nokia|цфо|on']})


async def test_preview_unknown_kind_is_rejected(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        with pytest.raises(ValueError, match='teapot'):
            await service.preview(db, {**PROBE_PAYLOAD, 'kind': 'teapot'})


async def test_preview_vless_is_an_estimate(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        preview = await service.preview(db, VLESS_PAYLOAD)
    assert preview.estimate_is_exact is False and preview.cost_kopeks == 5 * 110
    assert preview.request['raw_input'] == BS_LINK and preview.request['selected_modems'] == preview.units_resolved
    assert any('после запуска' in warning for warning in preview.warnings)


async def test_preview_scan_uses_cidr_and_exact_price(session_factory) -> None:
    service = make_service(session_factory)
    payload = {
        'kind': 'scan',
        'targets': [{'kind': 'cidr', 'value': '8.8.8.0/24'}, {'kind': 'host', 'ref': 'h-bs'}],
        'units': ['dobro|цфо|on'],
        'dpi': 'on',
        'probes': {'tcp': True, 'sni': True},
    }
    async with session_factory() as db:
        preview = await service.preview(db, payload)
        with pytest.raises(ValueError, match='/24'):
            await service.preview(db, {**payload, 'targets': [{'kind': 'host', 'ref': 'h-bs'}]})
    assert (preview.cost_kopeks, preview.estimate_is_exact) == (61, True)
    assert preview.request == {
        'cidr': '8.8.8.0/24',
        'operators': ['dobro|цфо|on'],
        'probes': {'icmp': False, 'tcp': True, 'sni': True},
        'dpi': 'on',
        'sni_hosts': ['whitelisted.example'],
    }


async def test_preview_without_units_left_warns(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        preview = await service.preview(db, {**PROBE_PAYLOAD, 'units': ['yota'], 'dpi': 'on'})
    assert preview.units_resolved == [] and any('ни одна симка' in warning for warning in preview.warnings)


# ---------------------------------------------------------------- запуск


async def test_create_job_writes_row_and_spawns_runner(session_factory) -> None:
    client = FakeClient({'probe': [load_bschek_fixture('p1_probe')['body']]})
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        job = await service.create_job(db, PROBE_PAYLOAD, admin.id)
        assert job.status == 'pending' and job.idempotency_key
        assert (job.units_requested, job.units_resolved, job.estimated_kopeks) == (['mts'], ['mts|пфо|on'], 18)
        assert job.targets[0]['target_key'] == 'bs-host.example:9443' and job.skipped['dpi_off']
        assert service.runner.is_active(job.id)
    await asyncio.gather(*service.runner._tasks.values())
    async with session_factory() as db:
        assert (await crud.get_job(db, job.id)).status == 'done'


async def test_create_job_returns_job_ready_for_response(session_factory) -> None:
    """Роут сериализует созданную задачу сразу, включая ``legs``.

    Свежий объект после flush/commit не имеет загруженной связи: обращение к
    ``job.legs`` в обработчике запускает ленивый SELECT вне greenlet — в проде
    это MissingGreenlet на POST /jobs. CRUD обязан отдавать задачу, готовую к
    ответу без дополнительного IO.
    """
    client = FakeClient({'probe': [load_bschek_fixture('p1_probe')['body']]})
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        job = await service.create_job(db, PROBE_PAYLOAD, admin.id)
        assert 'legs' not in sa_inspect(job).unloaded
        assert job.legs == []
    await asyncio.gather(*service.runner._tasks.values())


async def test_create_job_refuses_second_active_vless(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        admin = await _admin(db)
        await crud.create_job(
            db,
            kind='vless',
            status='running',
            trigger='manual',
            started_by_user_id=admin.id,
            idempotency_key='busy',
            request={},
            targets=[],
            dpi='on',
        )
        await db.commit()
        with pytest.raises(ReachabilityBusy) as excinfo:
            await service.create_job(db, VLESS_PAYLOAD, admin.id)
        assert excinfo.value.job.kind == 'vless'


async def test_create_job_enforces_cost_limit_and_units(session_factory) -> None:
    service = make_service(session_factory, limit=10)
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        with pytest.raises(CostLimitExceeded):
            await service.create_job(db, PROBE_PAYLOAD, admin.id)
        with pytest.raises(ValueError, match='симка'):
            await service.create_job(db, {**PROBE_PAYLOAD, 'units': ['yota']}, admin.id)
        assert (await crud.list_jobs(db))[1] == 0


async def test_create_job_refuses_when_balance_is_short(session_factory) -> None:
    client = FakeClient()
    poor = {**load_bschek_fixture('account')['body'], 'balance_total': 5}
    client.get_account = lambda: _coro(poor)  # type: ignore[method-assign]
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        with pytest.raises(ValueError, match='балансе'):
            await service.create_job(db, PROBE_PAYLOAD, admin.id)


async def _coro(value):
    return value


# ---------------------------------------------------------------- управление


async def test_get_cancel_and_retrieve_jobs(session_factory) -> None:
    client = FakeClient(
        {
            'cancel_vless': [load_bschek_fixture('vC_cancel')['body']],
            'get_vless': [load_bschek_fixture('vC_after_cancel')['body']],
        }
    )
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        admin = await _admin(db)
        job = await crud.create_job(
            db,
            kind='vless',
            status='running',
            trigger='manual',
            started_by_user_id=admin.id,
            idempotency_key='c',
            request={},
            targets=[],
            dpi='on',
            external_id=43306,
            result={'submit': load_bschek_fixture('vC_submit')['body']},
        )
        await db.commit()
        with pytest.raises(JobNotFound):
            await service.get_job(db, job.id + 100)
        cancelled = await service.cancel_job(db, job.id)
        assert cancelled.phase == 'cancelling' and service.runner.is_active(job.id)
    await asyncio.gather(*service.runner._tasks.values())
    async with session_factory() as db:
        assert (await crud.get_job(db, job.id)).status == 'cancelled'
        with pytest.raises(JobNotCancellable):
            await service.retrieve_job(db, job.id)


async def test_retrieve_job_resumes_stuck_probe(session_factory) -> None:
    client = FakeClient({'probe': [load_bschek_fixture('p1_probe')['body']]})
    service = make_service(session_factory, client=client)
    async with session_factory() as db:
        admin = await _admin(db)
        job = await crud.create_job(
            db,
            kind='probe',
            status='running',
            phase='retrieving',
            trigger='manual',
            started_by_user_id=admin.id,
            idempotency_key='r',
            request=load_bschek_fixture('p1_probe')['request'],
            targets=[],
            dpi='on',
        )
        await db.commit()
        await service.retrieve_job(db, job.id)
    await asyncio.gather(*service.runner._tasks.values())
    async with session_factory() as db:
        assert (await crud.get_job(db, job.id)).status == 'done'


async def test_summary_builds_matrix_from_latest_legs(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        admin = await _admin(db)
        job = await crud.create_job(
            db,
            kind='probe',
            status='done',
            trigger='manual',
            started_by_user_id=admin.id,
            idempotency_key='s',
            request={},
            targets=[],
            dpi='on',
        )
        leg = {
            'kind': 'probe',
            'target_key': 'bs-host.example:9443',
            'target_kind': 'host',
            'target_ref': 'h-bs',
            'op_key': 'mts|пфо|on',
            'operator': 'mts',
            'region': 'ПФО',
            'dpi': 'on',
            'verdict': 'reachable',
            'matches_expectation': True,
            'raw': {},
            'checked_at': datetime.now(UTC),
        }
        await crud.replace_legs(db, job.id, [leg])
        await db.commit()
        summary = await service.summary(db, dpi='on')
    row = summary['rows'][0]
    assert (row['target_key'], row['purpose'], row['cells']['mts|пфо|on']['verdict']) == (
        'bs-host.example:9443',
        'bs',
        'reachable',
    )
    assert 'mts|пфо|on' in [u['op_key'] for u in summary['units']]
    assert all(u['dpi'] == 'on' for u in summary['units'] if 'dpi' in u) and summary['panel_error'] is None


async def test_summary_survives_panel_and_api_outage(session_factory) -> None:
    client = FakeClient(account_error=BschekAPIError(code='unauthenticated', message='bad key', status=401))
    service = make_service(session_factory, client=client, panel=FakePanel(broken=True))
    async with session_factory() as db:
        await service.status(db)  # помечает нездоровье
        summary = await service.summary(db, dpi='any')
    assert summary['rows'] == [] and summary['units'] == [] and summary['panel_error']


async def test_update_pref_persists_and_changes_summary_purpose(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        admin = await _admin(db)
        await db.commit()
        pref = await service.update_pref(
            db,
            target_kind='host',
            target_ref='h-bs',
            purpose='regular',
            excluded=False,
            note='обычный',
            admin_id=admin.id,
        )
        assert (pref.purpose, pref.updated_by_user_id) == ('regular', admin.id)
        hosts = await service.hosts(db)
    assert (hosts[0].target.purpose, hosts[0].purpose_guessed) == ('regular', False)


async def test_background_sweeper_starts_and_stops(session_factory) -> None:
    service = make_service(session_factory)
    service.start_background()
    assert service._background is not None and not service._background.done()
    await service.stop_background()
    assert service._background is None


# ---------------------------------------------------------------- SNI: дефолт из настроек и свои имена


async def test_preview_probe_uses_explicit_sni_hosts_for_bare_ip(session_factory) -> None:
    service = make_service(session_factory)
    payload = {
        'kind': 'probe',
        'targets': [{'kind': 'custom', 'value': '8.8.8.8'}],
        'units': ['mts'],
        'dpi': 'on',
        'probes': {'tcp': True, 'sni': True},
        'sni_hosts': ['ads.x5.ru', 'vk.com'],
    }
    async with session_factory() as db:
        preview = await service.preview(db, payload)
    assert preview.request['sni_hosts'] == ['ads.x5.ru', 'vk.com']


async def test_preview_probe_falls_back_to_built_in_default_sni(session_factory) -> None:
    service = make_service(session_factory)
    payload = {
        'kind': 'probe',
        'targets': [{'kind': 'custom', 'value': '8.8.8.8'}],
        'units': ['mts'],
        'dpi': 'on',
        'probes': {'tcp': True, 'sni': True},
    }
    async with session_factory() as db:
        preview = await service.preview(db, payload)
        status = await service.status(db)
    assert preview.request['sni_hosts'] == ['ads.x5.ru']
    assert status['default_sni'] == 'ads.x5.ru'


async def test_preview_scan_with_sni_takes_names_from_payload(session_factory) -> None:
    service = make_service(session_factory)
    payload = {
        'kind': 'scan',
        'targets': [{'kind': 'cidr', 'value': '8.8.8.0/24'}],
        'units': ['dobro|цфо|on'],
        'dpi': 'on',
        'probes': {'tcp': True, 'sni': True},
        'sni_hosts': ['ads.x5.ru'],
    }
    async with session_factory() as db:
        preview = await service.preview(db, payload)
    assert preview.request['sni_hosts'] == ['ads.x5.ru']


# ---------------------------------------------------------------- поле «Конфиг или подписка»

EU_LINK = 'vless://00000000-0000-4000-8000-000000000001@eu-host.example:443?security=reality&sni=eu-host.example#EU'


async def test_parse_input_direct_links_become_custom_targets(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        parsed = await service.parse_input(db, f'{EU_LINK}\n8.8.8.8\n')
    assert [c.target.target_key for c in parsed.configs] == ['eu-host.example:443']
    assert parsed.configs[0].target_in == {'kind': 'custom', 'value': EU_LINK}
    assert parsed.configs[0].target.raw_link == EU_LINK
    assert [r.reason for r in parsed.rejected] == ['unsupported_scheme']
    assert parsed.sources == [{'kind': 'links', 'label': 'ссылки', 'count': 1}]


async def test_parse_input_own_panel_url_resolves_through_panel_api(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        parsed = await service.parse_input(db, 'https://sub.example/ref-1')
    config = parsed.configs[0]
    assert config.target.kind == 'subscription_config' and config.target.raw_link == BS_LINK
    assert config.target_in == {
        'kind': 'subscription_config',
        'short_uuid': 'ref-1',
        'index': 0,
        'target_key': 'bs-host.example:9443',
    }
    assert parsed.sources == [{'kind': 'subscription', 'label': 'https://sub.example/ref-1', 'count': 1}]


async def test_parse_input_foreign_url_is_fetched_and_referenced_by_url(session_factory) -> None:
    url = 'https://other.example/xyz'
    service = make_service(session_factory, url_links={url: [EU_LINK]})
    async with session_factory() as db:
        parsed = await service.parse_input(db, url)
        # Цель по url разрешается при preview — та же ссылка, что при разборе.
        preview = await service.preview(
            db,
            {
                'kind': 'vless',
                'targets': [parsed.configs[0].target_in],
                'units': ['*|цфо|on'],
                'dpi': 'on',
                'probes': {},
                'core': '',
            },
        )
    assert parsed.configs[0].target_in == {
        'kind': 'subscription_config',
        'url': url,
        'index': 0,
        'target_key': 'eu-host.example:443',
    }
    assert preview.request['raw_input'] == EU_LINK


async def test_parse_input_unreachable_url_is_rejected_not_raised(session_factory) -> None:
    service = make_service(session_factory)
    async with session_factory() as db:
        parsed = await service.parse_input(db, 'https://dead.example/abc')
    assert parsed.configs == [] and [r.reason for r in parsed.rejected] == ['subscription_failed']
    assert parsed.rejected[0].raw == 'https://dead.example/abc'


async def test_parse_input_base64_blob_expands_to_links(session_factory) -> None:
    import base64

    service = make_service(session_factory)
    blob = base64.b64encode(f'{EU_LINK}\n{BS_LINK}'.encode()).decode()
    async with session_factory() as db:
        parsed = await service.parse_input(db, blob)
    assert [c.target.target_key for c in parsed.configs] == ['eu-host.example:443', 'bs-host.example:9443']
