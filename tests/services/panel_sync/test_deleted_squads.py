"""TG120: a deleted internal squad must not block remaining assigned servers."""

from types import SimpleNamespace

import pytest

from app.external.remnawave_api import RemnaWaveAPIError, RemnaWaveTransientError
from app.services.panel_sync import push_subscription
from app.services.panel_sync.identity import PanelIdentity
from tests.services.panel_sync.test_writer import NOW, _panel_user, _sub, _user


class Panel:
    def __init__(self, *, existing=('live', 'unrelated'), error_code='A018', catalog_error=None):
        self.existing = existing
        self.error = RemnaWaveAPIError('Write failed', 500, {'errorCode': error_code})
        self.catalog_error = catalog_error
        self.calls = []
        self.reads = 0
        self.saved = None

    async def get_internal_squads(self):
        self.reads += 1
        if self.catalog_error:
            raise self.catalog_error
        return [SimpleNamespace(uuid=uuid) for uuid in self.existing]

    async def write(self, **kwargs):
        self.calls.append(kwargs)
        if any(s not in self.existing for s in kwargs.get('active_internal_squads', [])):
            raise self.error
        self.saved = kwargs
        return _panel_user()

    create_user = write
    update_user = write

    async def get_user_devices_all(self, user_id):
        return {'total': 0, 'devices': []}


@pytest.mark.parametrize('multi', [False, True])
@pytest.mark.parametrize('panel_id,code', [(None, 'A018'), (42, 'A039'), (42, 'A118')])
async def test_deleted_squad_recovery_preserves_only_assigned_live_servers(multi, panel_id, code):
    api = Panel(error_code=code)
    sub = _sub(connected_squads=['live', 'deleted'])
    original_end = sub.end_date
    result = await push_subscription(
        api,
        _user(),
        sub,
        identity=PanelIdentity(panel_user=_panel_user(panel_id) if panel_id else None),
        multi_tariff=multi,
        now=NOW,
        reset_devices=False,
    )
    assert result.action == ('created' if panel_id is None else 'updated')
    assert api.saved['active_internal_squads'] == ['live']
    assert api.saved == {**api.calls[0], 'active_internal_squads': ['live']}
    assert len(api.calls) == 2
    assert api.reads == 1
    assert sub.end_date == original_end
    # No DB-side tariff/entitlement rewrite from a failed remote attempt.
    assert sub.connected_squads == ['live', 'deleted']


@pytest.mark.parametrize('panel_id', [None, 42])
@pytest.mark.parametrize('existing', [(), ('unrelated',)])
async def test_all_assigned_squads_deleted_does_not_grant_unrelated_access(panel_id, existing):
    api = Panel(existing=existing)
    with pytest.raises(RemnaWaveAPIError) as caught:
        await push_subscription(
            api,
            _user(),
            _sub(connected_squads=['deleted']),
            identity=PanelIdentity(panel_user=_panel_user(panel_id) if panel_id else None),
            multi_tariff=True,
            now=NOW,
            reset_devices=False,
        )
    assert caught.value is api.error
    assert api.saved is None
    assert len(api.calls) == 1


@pytest.mark.parametrize('code', ['A025', 'A063', 'A182', 'UNKNOWN'])
async def test_other_errors_do_not_trigger_squad_recovery(code):
    api = Panel(error_code=code)
    with pytest.raises(RemnaWaveAPIError):
        await push_subscription(
            api,
            _user(),
            _sub(connected_squads=['live', 'deleted']),
            identity=PanelIdentity(),
            multi_tariff=True,
            now=NOW,
        )
    assert api.reads == 0
    assert len(api.calls) == 1


async def test_catalog_outage_preserves_original_failure_without_retry():
    api = Panel(catalog_error=RemnaWaveTransientError('unavailable'))
    with pytest.raises(RemnaWaveAPIError) as caught:
        await push_subscription(
            api,
            _user(),
            _sub(connected_squads=['live', 'deleted']),
            identity=PanelIdentity(),
            multi_tariff=True,
            now=NOW,
        )
    assert caught.value is api.error
    assert len(api.calls) == 1


async def test_normal_write_does_not_fetch_catalog():
    api = Panel()
    await push_subscription(
        api,
        _user(),
        _sub(connected_squads=['live']),
        identity=PanelIdentity(),
        multi_tariff=True,
        now=NOW,
    )
    assert api.saved['active_internal_squads'] == ['live']
    assert api.reads == 0


@pytest.mark.parametrize('catalog', [None, {}, [SimpleNamespace(uuid=None)], [SimpleNamespace(uuid='')]])
async def test_invalid_catalog_cannot_authorize_a_retry(catalog):
    api = Panel()

    async def invalid_catalog():
        return catalog

    api.get_internal_squads = invalid_catalog
    with pytest.raises(RemnaWaveAPIError) as caught:
        await push_subscription(
            api,
            _user(),
            _sub(connected_squads=['live', 'deleted']),
            identity=PanelIdentity(),
            multi_tariff=True,
            now=NOW,
        )
    assert caught.value is api.error
    assert len(api.calls) == 1


async def test_generic_error_with_unchanged_catalog_is_not_retried():
    api = Panel(existing=('live', 'deleted'))

    async def fail(**kwargs):
        api.calls.append(kwargs)
        raise api.error

    api.create_user = fail
    with pytest.raises(RemnaWaveAPIError) as caught:
        await push_subscription(
            api,
            _user(),
            _sub(connected_squads=['live', 'deleted']),
            identity=PanelIdentity(),
            multi_tariff=True,
            now=NOW,
        )
    assert caught.value is api.error
    assert api.reads == 1
    assert len(api.calls) == 1


async def test_second_failure_is_propagated_without_retry_loop():
    api = Panel()
    second_error = RemnaWaveAPIError('Second failure', 500, {'errorCode': 'A018'})

    async def fail(**kwargs):
        api.calls.append(kwargs)
        raise api.error if len(api.calls) == 1 else second_error

    with pytest.raises(RemnaWaveAPIError) as caught:
        await push_subscription(
            api,
            _user(),
            _sub(connected_squads=['live', 'deleted']),
            identity=PanelIdentity(),
            multi_tariff=True,
            now=NOW,
            create_call=fail,
        )
    assert caught.value is second_error
    assert api.reads == 1
    assert len(api.calls) == 2


@pytest.mark.parametrize('panel_id', [None, 42])
async def test_recovery_through_real_http_client_and_catalog_parser(panel_id):
    from unittest.mock import AsyncMock

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from app.external.remnawave_api import RemnaWaveAPI
    from tests.external.test_remnawave_3_4_3_contract import _user_payload

    calls = []

    async def write(request):
        payload = await request.json()
        calls.append(payload)
        if 'deleted' in payload.get('activeInternalSquads', []):
            return web.json_response(
                {'errorCode': 'A018' if request.method == 'POST' else 'A039', 'message': 'Write failed'},
                status=500,
            )
        return web.json_response({'response': _user_payload(activeInternalSquads=[{'uuid': 'live'}])})

    async def catalog(request):
        return web.json_response(
            {'response': {'internalSquads': [{'uuid': 'live', 'name': 'Live'}, {'uuid': 'unrelated', 'name': 'Other'}]}}
        )

    app = web.Application()
    app.router.add_route('*', '/api/users', write)
    app.router.add_get('/api/internal-squads', catalog)
    async with TestServer(app) as server, RemnaWaveAPI(str(server.make_url('')).rstrip('/'), 'test-key') as api:
        api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda user: user)
        result = await push_subscription(
            api,
            _user(),
            _sub(connected_squads=['live', 'deleted'], device_limit=0),
            identity=PanelIdentity(known_id=panel_id),
            multi_tariff=True,
            now=NOW,
            reset_devices=False,
        )
    assert result.panel_user.id == 42
    assert result.panel_user.active_internal_squads == [{'uuid': 'live'}]
    assert len(calls) == 2
    assert calls[1] == {**calls[0], 'activeInternalSquads': ['live']}
