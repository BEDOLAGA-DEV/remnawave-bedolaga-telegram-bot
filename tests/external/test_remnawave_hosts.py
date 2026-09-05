"""GET /api/hosts — то, куда подключаются пользователи (адрес, порт, SNI, инбаунд).

У ноды address/port — канал «панель → нода» (порт агента), поэтому проверять
достижимость надо по хостам. Форма объекта хоста взята из схемы панели.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveHost


HOST_PAYLOAD = {
    'uuid': 'h-1',
    'viewPosition': 2,
    'remark': '🇩🇪 Germany',
    'address': 'eu-host.example',
    'port': 443,
    'path': None,
    'sni': 'eu-host.example',
    'host': None,
    'alpn': None,
    'fingerprint': 'firefox',
    'isDisabled': False,
    'isHidden': False,
    'securityLayer': 'DEFAULT',
    'tag': 'EU',
    'inbound': {'configProfileUuid': 'cp-1', 'configProfileInboundUuid': 'in-1'},
}


def test_parse_host_maps_panel_fields() -> None:
    host = RemnaWaveAPI._parse_host(HOST_PAYLOAD)
    assert host == RemnaWaveHost(
        uuid='h-1',
        remark='🇩🇪 Germany',
        address='eu-host.example',
        port=443,
        sni='eu-host.example',
        host=None,
        is_disabled=False,
        is_hidden=False,
        tag='EU',
        security_layer='DEFAULT',
        config_profile_uuid='cp-1',
        config_profile_inbound_uuid='in-1',
        view_position=2,
    )


def test_parse_host_tolerates_missing_optional_fields() -> None:
    host = RemnaWaveAPI._parse_host({'uuid': 'h-2', 'remark': 'x', 'address': 'a.example'})
    assert (host.port, host.sni, host.config_profile_inbound_uuid, host.is_disabled) == (None, None, None, False)


async def test_get_all_hosts_calls_hosts_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RemnaWaveAPI(base_url='https://panel.example', api_key='k')
    calls: list[tuple[str, str]] = []

    async def fake_make_request(
        method: str, endpoint: str, data: dict | None = None, params: dict | None = None
    ) -> Any:
        calls.append((method, endpoint))
        return {'response': [HOST_PAYLOAD]}

    monkeypatch.setattr(api, '_make_request', fake_make_request)
    hosts = await api.get_all_hosts()
    assert calls == [('GET', '/api/hosts')]
    assert [h.uuid for h in hosts] == ['h-1']


NODE_PAYLOAD = {
    'uuid': 'n-1',
    'name': 'DE-1',
    'address': '192.0.2.142',
    'countryCode': 'DE',
    'isConnected': True,
    'isDisabled': False,
    'usersOnline': 0,
    'configProfile': {
        'activeConfigProfileUuid': 'cp-1',
        'activeInbounds': [
            {'uuid': 'in-1', 'profileUuid': 'cp-1', 'tag': 'VLESS_REALITY', 'type': 'vless'},
            {'uuid': 'in-2', 'profileUuid': 'cp-1', 'tag': 'SS', 'type': 'shadowsocks'},
            {'tag': 'без uuid — пропускается'},
        ],
    },
}


def test_parse_node_exposes_active_inbound_uuids_for_host_linking() -> None:
    node = RemnaWaveAPI(base_url='https://panel.example', api_key='k')._parse_node(NODE_PAYLOAD)
    assert (node.active_config_profile_uuid, node.active_inbound_uuids) == ('cp-1', ['in-1', 'in-2'])


def test_parse_node_without_config_profile_has_no_inbounds() -> None:
    payload = {key: value for key, value in NODE_PAYLOAD.items() if key != 'configProfile'}
    node = RemnaWaveAPI(base_url='https://panel.example', api_key='k')._parse_node(payload)
    assert (node.active_config_profile_uuid, node.active_inbound_uuids) == (None, [])
