"""Цели из пяти источников приводятся к одному формату; ссылки — только через панель."""

from __future__ import annotations

import pytest

from app.external.remnawave_api import RemnaWaveHost, RemnaWaveNode
from app.services.reachability.resolver import TargetResolutionError, TargetResolver
from app.services.reachability.targets import (
    KIND_CIDR,
    KIND_CUSTOM,
    KIND_HOST,
    KIND_NODE,
    KIND_SUBSCRIPTION_CONFIG,
)


UUID = '00000000-0000-4000-8000-000000000001'
BS_LINK = f'vless://{UUID}@bs-host.example:9443?security=reality&sni=whitelisted.example#BS'
EU_LINK = f'vless://{UUID}@eu-host.example:443?security=reality&sni=eu-host.example#EU'
STUB = f'vless://{UUID}@0.0.0.0:1?security=none#stub'

HOSTS = [
    RemnaWaveHost(
        uuid='h-bs',
        remark='RU | LTE | БС',
        address='bs-host.example',
        port=9443,
        sni='whitelisted.example',
        config_profile_inbound_uuid='in-bs',
    ),
    RemnaWaveHost(
        uuid='h-eu',
        remark='Germany',
        address='eu-host.example',
        port=443,
        sni='eu-host.example',
        config_profile_inbound_uuid='in-eu',
    ),
    RemnaWaveHost(uuid='h-off', remark='Old', address='old.example', port=443, is_disabled=True),
]
NODES = [
    RemnaWaveNode(
        uuid='n-1',
        name='DE-1',
        address='192.0.2.142',
        country_code='DE',
        is_connected=True,
        is_disabled=False,
        users_online=0,
        traffic_used_bytes=0,
        traffic_limit_bytes=None,
        port=2222,
        active_inbound_uuids=['in-eu'],
    ),
]


def _resolver(prefs: dict | None = None, links: list[str] | None = None) -> TargetResolver:
    async def fetch_hosts():
        return HOSTS

    async def fetch_nodes():
        return NODES

    async def fetch_links(short_uuid: str):
        assert short_uuid == 'sub-1'
        return links if links is not None else [BS_LINK, EU_LINK, STUB]

    return TargetResolver(fetch_hosts=fetch_hosts, fetch_nodes=fetch_nodes, fetch_links=fetch_links, prefs=prefs or {})


async def test_hosts_hide_disabled_by_default_and_guess_purpose() -> None:
    views = await _resolver().hosts()
    assert [v.host.uuid for v in views] == ['h-bs', 'h-eu']
    bs, eu = views
    assert (bs.target.kind, bs.target.target_key, bs.target.sni, bs.target.purpose, bs.purpose_guessed) == (
        KIND_HOST,
        'bs-host.example:9443',
        'whitelisted.example',
        'bs',
        True,
    )
    assert (eu.target.purpose, eu.node_uuids) == ('regular', ['n-1'])
    assert len(await _resolver().hosts(include_disabled=True)) == 3


async def test_sources_are_fetched_once_per_resolver() -> None:
    calls = {'hosts': 0, 'nodes': 0}

    async def fetch_hosts():
        calls['hosts'] += 1
        return HOSTS

    async def fetch_nodes():
        calls['nodes'] += 1
        return NODES

    async def fetch_links(short_uuid: str):
        return []

    resolver = TargetResolver(fetch_hosts=fetch_hosts, fetch_nodes=fetch_nodes, fetch_links=fetch_links, prefs={})
    await resolver.hosts()
    await resolver.nodes()
    await resolver.resolve([{'kind': 'host', 'ref': 'h-bs'}, {'kind': 'node', 'ref': 'n-1'}])
    assert calls == {'hosts': 1, 'nodes': 1}


async def test_prefs_override_guess_and_mark_excluded() -> None:
    views = await _resolver(prefs={('host', 'h-bs'): ('regular', True)}).hosts()
    assert (views[0].target.purpose, views[0].purpose_guessed, views[0].excluded) == ('regular', False, True)


async def test_pref_with_unknown_purpose_keeps_guess() -> None:
    views = await _resolver(prefs={('host', 'h-bs'): ('unknown', False)}).hosts()
    assert (views[0].target.purpose, views[0].purpose_guessed, views[0].excluded) == ('bs', True, False)


async def test_nodes_expose_icmp_target_and_linked_hosts() -> None:
    views = await _resolver().nodes()
    assert views[0].target.kind == KIND_NODE
    assert (views[0].target.address, views[0].target.port, views[0].host_uuids) == ('192.0.2.142', None, ['h-eu'])


async def test_subscription_configs_parse_links_and_reject_stubs() -> None:
    configs = await _resolver().subscription_configs('sub-1')
    assert [c.label for c in configs.configs] == ['BS', 'EU']
    assert configs.configs[0].kind == KIND_SUBSCRIPTION_CONFIG
    assert configs.configs[0].raw_link == BS_LINK
    assert configs.configs[0].purpose == 'bs'
    assert configs.configs[0].ref == {'short_uuid': 'sub-1', 'index': 0}
    assert [r.reason for r in configs.rejected] == ['stub']


async def test_resolve_mixed_items_dedups_by_target_key() -> None:
    targets = await _resolver().resolve(
        [
            {'kind': 'host', 'ref': 'h-bs'},
            {'kind': 'custom', 'value': 'BS-HOST.example:9443'},
            {'kind': 'node', 'ref': 'n-1'},
            {'kind': 'subscription_config', 'short_uuid': 'sub-1', 'index': 1},
            {'kind': 'cidr', 'value': '8.8.8.77/24'},
        ]
    )
    assert [t.kind for t in targets] == [KIND_HOST, KIND_NODE, KIND_SUBSCRIPTION_CONFIG, KIND_CIDR]
    assert targets[-1].target_key == '8.8.8.0/24'


async def test_resolve_host_applies_prefs() -> None:
    targets = await _resolver(prefs={('host', 'h-bs'): ('regular', False)}).resolve([{'kind': 'host', 'ref': 'h-bs'}])
    assert targets[0].purpose == 'regular'


async def test_resolve_custom_link_becomes_config_target() -> None:
    targets = await _resolver().resolve([{'kind': 'custom', 'value': EU_LINK}])
    assert (targets[0].kind, targets[0].raw_link, targets[0].target_key) == (
        KIND_CUSTOM,
        EU_LINK,
        'eu-host.example:443',
    )


@pytest.mark.parametrize(
    'item',
    [
        {'kind': 'host', 'ref': 'missing'},
        {'kind': 'node', 'ref': 'missing'},
        {'kind': 'subscription_config', 'short_uuid': 'sub-1', 'index': 7},
        {'kind': 'subscription_config', 'short_uuid': 'sub-1', 'index': '0'},
        {'kind': 'custom', 'value': '10.0.0.1'},
        {'kind': 'custom', 'value': STUB},
        {'kind': 'cidr', 'value': '8.8.8.0/23'},
        {'kind': 'teapot', 'value': 'x'},
    ],
)
async def test_resolve_reports_unknown_targets(item: dict) -> None:
    with pytest.raises((TargetResolutionError, ValueError)):
        await _resolver().resolve([item])


# ---------------------------------------------------------------- подписка по URL (чужая панель)


def _url_resolver(links: list[str] | None = None) -> tuple[TargetResolver, list[str]]:
    calls: list[str] = []

    async def fetch_hosts():
        return HOSTS

    async def fetch_nodes():
        return NODES

    async def fetch_links(short_uuid: str):
        return [BS_LINK]

    async def fetch_url_links(url: str):
        calls.append(url)
        return links if links is not None else [EU_LINK]

    resolver = TargetResolver(
        fetch_hosts=fetch_hosts,
        fetch_nodes=fetch_nodes,
        fetch_links=fetch_links,
        fetch_url_links=fetch_url_links,
        prefs={},
    )
    return resolver, calls


async def test_subscription_configs_from_url_use_url_fetcher_and_url_refs() -> None:
    resolver, calls = _url_resolver()
    url = 'https://sub.example/abc'
    configs = await resolver.subscription_configs(url)
    assert configs.short_uuid == url
    assert configs.configs[0].ref == {'url': url, 'index': 0}
    targets = await resolver.resolve(
        [{'kind': 'subscription_config', 'url': url, 'index': 0, 'target_key': 'eu-host.example:443'}]
    )
    assert targets[0].raw_link == EU_LINK and targets[0].kind == KIND_SUBSCRIPTION_CONFIG
    assert calls == [url]  # второй раз — из кэша резолвера


async def test_subscription_config_with_stale_target_key_is_rejected() -> None:
    resolver, _ = _url_resolver()
    with pytest.raises(TargetResolutionError, match='изменилась'):
        await resolver.resolve(
            [
                {
                    'kind': 'subscription_config',
                    'url': 'https://sub.example/abc',
                    'index': 0,
                    'target_key': 'other.example:1',
                }
            ]
        )


async def test_url_source_without_fetcher_is_explained() -> None:
    with pytest.raises(TargetResolutionError, match='URL'):
        await _resolver().subscription_configs('https://sub.example/abc')
