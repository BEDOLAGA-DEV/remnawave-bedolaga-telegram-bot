"""Цели DPI//CHECKER из панели: ключи подписки пользователя с именами, адреса хостов и нод без дублей."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.dpichecker import targets


def _client(**methods):
    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(**methods)

    return factory


async def test_subscription_keys_named_by_remark(monkeypatch):
    async def short_uuid(db, user_id):
        assert user_id == 5
        return 'su-1'

    async def links(api, short_uuid, prefer_public=False):
        assert short_uuid == 'su-1'
        return [
            'vless://u@fi.example:443?security=reality#%F0%9F%87%AB%F0%9F%87%AE%20Finland',
            'trojan://p@de.example:8443',
            'https://not-a-key.example/page',
        ]

    monkeypatch.setattr(targets, 'short_uuid_for_user', short_uuid)
    monkeypatch.setattr(targets, 'fetch_panel_links', links)
    keys = await targets.subscription_keys(None, user_id=5, panel_client=_client())
    assert [k.name for k in keys] == ['🇫🇮 Finland', 'de.example:8443']
    assert keys[0].value.startswith('vless://') and keys[0].ref == 'su-1'


async def test_user_without_subscription_is_explained(monkeypatch):
    async def none(db, user_id):
        return None

    monkeypatch.setattr(targets, 'short_uuid_for_user', none)
    with pytest.raises(targets.PanelTargetError, match='#5'):
        await targets.subscription_keys(None, user_id=5, panel_client=_client())


async def test_empty_subscription_is_explained(monkeypatch):
    async def short_uuid(db, user_id):
        return 'su-1'

    async def links(api, short_uuid, prefer_public=False):
        return []

    monkeypatch.setattr(targets, 'short_uuid_for_user', short_uuid)
    monkeypatch.setattr(targets, 'fetch_panel_links', links)
    with pytest.raises(targets.PanelTargetError, match='ключей'):
        await targets.subscription_keys(None, user_id=5, panel_client=_client())


async def test_host_addresses_deduplicated_and_only_chosen():
    hosts = [
        SimpleNamespace(uuid='h1', remark='Finland', address='fi.example'),
        SimpleNamespace(uuid='h2', remark='Finland 2', address='FI.example'),
        SimpleNamespace(uuid='h3', remark='Other', address='x.example'),
    ]

    async def get_all_hosts():
        return hosts

    found = await targets.host_addresses(panel_client=_client(get_all_hosts=get_all_hosts), host_uuids=['h1', 'h2'])
    assert [(t.value, t.name, t.ref) for t in found] == [('fi.example', 'Finland', 'h1')]


async def test_node_addresses_named_by_node():
    nodes = [SimpleNamespace(uuid='n1', name='NL-1', address='203.0.113.5')]

    async def get_all_nodes():
        return nodes

    found = await targets.node_addresses(panel_client=_client(get_all_nodes=get_all_nodes), node_uuids=['n1', 'nX'])
    assert [(t.value, t.name) for t in found] == [('203.0.113.5', 'NL-1')]


async def test_unknown_uuids_only_is_an_error():
    async def get_all_hosts():
        return []

    with pytest.raises(targets.PanelTargetError):
        await targets.host_addresses(panel_client=_client(get_all_hosts=get_all_hosts), host_uuids=['zzz'])


async def test_empty_choice_lists_all_live_hosts_for_the_picker():
    hosts = [
        SimpleNamespace(uuid='h1', remark='Finland', address='fi.example', is_disabled=False),
        SimpleNamespace(uuid='h2', remark='Off', address='off.example', is_disabled=True),
    ]

    async def get_all_hosts():
        return hosts

    found = await targets.host_addresses(panel_client=_client(get_all_hosts=get_all_hosts), host_uuids=[])
    assert [(t.value, t.ref) for t in found] == [('fi.example', 'h1')]


async def test_empty_choice_lists_all_nodes():
    nodes = [SimpleNamespace(uuid='n1', name='NL-1', address='nl.example')]

    async def get_all_nodes():
        return nodes

    found = await targets.node_addresses(panel_client=_client(get_all_nodes=get_all_nodes), node_uuids=[])
    assert [t.name for t in found] == ['NL-1']


async def test_subscription_keys_by_short_uuid_without_user(monkeypatch):
    """Подписка по умолчанию из настроек — сразу shortUuid, пользователь не нужен."""

    async def short_uuid(db, user_id):
        raise AssertionError('пользователь не выбирался')

    async def links(api, short_uuid, prefer_public=False):
        assert short_uuid == 'ref-1'
        return ['vless://u@fi.example:443#Finland']

    monkeypatch.setattr(targets, 'short_uuid_for_user', short_uuid)
    monkeypatch.setattr(targets, 'fetch_panel_links', links)
    keys = await targets.subscription_keys(None, short_uuid='ref-1', panel_client=_client())
    assert [(k.name, k.ref) for k in keys] == [('Finland', 'ref-1')]
