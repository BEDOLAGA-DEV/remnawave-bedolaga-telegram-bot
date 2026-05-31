from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.speedtest_service as ss
from app.services.speedtest_service import SpeedtestService


_NODES = [
    {'uuid': 'u1', 'name': 'NL-1', 'address': '1.2.3.4', 'country_code': 'NL',
     'is_node_online': True, 'users_online': 5},
    {'uuid': 'u2', 'name': 'DE-1', 'address': '5.6.7.8', 'country_code': 'DE',
     'is_node_online': False, 'users_online': 0},
]


@pytest.fixture
def service():
    svc = SpeedtestService()
    svc._remnawave = MagicMock()
    svc._remnawave.get_all_nodes = AsyncMock(return_value=[dict(n) for n in _NODES])
    svc._nodes_cache = None
    svc._nodes_cache_at = None
    return svc


@pytest.mark.asyncio
async def test_mapping_resolves_host(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com', 'u2': 'de1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    targets = await service.get_ping_targets()
    hosts = {t['ping_host'] for t in targets}
    assert hosts == {'nl1.example.com', 'de1.example.com'}
    assert all('address' not in t for t in targets)


@pytest.mark.asyncio
async def test_node_without_host_excluded(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    targets = await service.get_ping_targets()
    assert [t['ping_host'] for t in targets] == ['nl1.example.com']


@pytest.mark.asyncio
async def test_template_resolves_host(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping', classmethod(lambda cls: {}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '{node_name}.vpn.example.com', raising=False)
    targets = await service.get_ping_targets()
    hosts = sorted(t['ping_host'] for t in targets)
    assert hosts == ['DE-1.vpn.example.com', 'NL-1.vpn.example.com']


@pytest.mark.asyncio
async def test_cache_avoids_second_fetch(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    await service.get_ping_targets()
    await service.get_ping_targets()
    assert service._remnawave.get_all_nodes.await_count == 1


@pytest.mark.asyncio
async def test_target_shape(service, monkeypatch):
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    targets = await service.get_ping_targets()
    t = targets[0]
    assert set(t.keys()) == {'name', 'country_code', 'ping_host', 'is_online', 'users_online'}


@pytest.mark.asyncio
async def test_template_sanitizes_hostile_node_name(service, monkeypatch):
    # Hostile node name with a space/path must not produce an injectable ping_host.
    hostile = [
        {'uuid': 'h1', 'name': 'evil.com/x', 'country_code': 'NL', 'is_node_online': True, 'users_online': 0},
        {'uuid': 'h2', 'name': 'has space', 'country_code': 'DE', 'is_node_online': True, 'users_online': 0},
    ]
    service._remnawave.get_all_nodes = AsyncMock(return_value=[dict(n) for n in hostile])
    service._nodes_cache = None
    service._nodes_cache_at = None
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping', classmethod(lambda cls: {}))
    # bare {node_name} template — a sane template would suffix a domain, but this
    # proves the template result is sanitized, not returned raw.
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '{node_name}', raising=False)
    targets = await service.get_ping_targets()
    # 'evil.com/x' → sanitized to 'evil.com' (path stripped); 'has space' → rejected (None, excluded).
    hosts = [t['ping_host'] for t in targets]
    assert all(' ' not in h and '/' not in h for h in hosts)
    assert 'has space' not in hosts


@pytest.mark.asyncio
async def test_disabled_node_excluded(service, monkeypatch):
    nodes = [
        {'uuid': 'u1', 'name': 'NL-1', 'country_code': 'NL', 'is_node_online': True, 'users_online': 1, 'is_disabled': False},
        {'uuid': 'u2', 'name': 'DE-1', 'country_code': 'DE', 'is_node_online': True, 'users_online': 1, 'is_disabled': True},
    ]
    service._remnawave.get_all_nodes = AsyncMock(return_value=[dict(n) for n in nodes])
    service._nodes_cache = None
    service._nodes_cache_at = None
    monkeypatch.setattr(ss.SpeedtestSettingsService, 'get_host_mapping',
                        classmethod(lambda cls: {'u1': 'nl1.example.com', 'u2': 'de1.example.com'}))
    monkeypatch.setattr(ss.settings, 'SPEEDTEST_PING_HOST_TEMPLATE', '', raising=False)
    targets = await service.get_ping_targets()
    assert [t['ping_host'] for t in targets] == ['nl1.example.com']  # disabled u2 excluded
