"""Тела запросов к API из целей и симок: строгий селектор проб, SNI парой, лимит 20 конфигов."""

from __future__ import annotations

import pytest

from app.services.reachability.links import MAX_CONFIGS_PER_TEST
from app.services.reachability.requests import (
    MAX_PROBE_TARGETS,
    RequestBuildError,
    build_probe_request,
    build_scan_request,
    build_vless_request,
)
from app.services.reachability.targets import Target


def _t(address: str, port: int | None, sni: str | None, raw: str | None = None, kind: str = 'host') -> Target:
    key = f'{address}:{port}' if port else address
    return Target(kind=kind, label=address, address=address, port=port, target_key=key, sni=sni, raw_link=raw)


def test_probe_request_has_targets_units_probes_and_sni_hosts() -> None:
    body = build_probe_request(
        [_t('bs-host.example', 9443, 'whitelisted.example'), _t('eu-host.example', None, 'eu-host.example')],
        ['mts|цфо|on'],
        'on',
        {'icmp': False, 'tcp': True, 'sni': True},
    )
    assert body == {
        'targets': ['bs-host.example:9443', 'eu-host.example'],
        'operators': ['mts|цфо|on'],
        'probes': {'icmp': False, 'tcp': True, 'sni': True},
        'dpi': 'on',
        'sni_hosts': ['eu-host.example', 'whitelisted.example'],
    }


def test_probe_request_without_sni_omits_sni_hosts_and_skips_cidr_targets() -> None:
    body = build_probe_request(
        [_t('eu-host.example', None, 'eu-host.example'), _t('192.0.2.0', None, None, kind='cidr')],
        [],
        'off',
        {'icmp': True, 'tcp': True, 'sni': False},
    )
    assert body['targets'] == ['eu-host.example'] and 'sni_hosts' not in body and body['operators'] == []


def test_probe_request_normalizes_partial_probes_dict() -> None:
    body = build_probe_request([_t('a.example', None, None)], [], 'on', {'tcp': True})
    assert body['probes'] == {'icmp': False, 'tcp': True, 'sni': False}


def test_probe_request_rejects_no_probes_and_no_targets() -> None:
    with pytest.raises(RequestBuildError):
        build_probe_request([_t('a.example', None, None)], [], 'on', {'icmp': False, 'tcp': False, 'sni': False})
    with pytest.raises(RequestBuildError):
        build_probe_request([], [], 'on', {'tcp': True})
    with pytest.raises(RequestBuildError):
        build_probe_request([_t('192.0.2.0', None, None, kind='cidr')], [], 'on', {'tcp': True})


def test_vless_request_joins_raw_links_and_limits_20() -> None:
    links = [
        _t(f's{i}.example', 443, None, raw=f'vless://u@s{i}.example:443#s{i}', kind='subscription_config')
        for i in range(3)
    ]
    body = build_vless_request(links, ['mts|*|off'], 'any', 'stable')
    assert body == {
        'raw_input': '\n'.join(t.raw_link for t in links),
        'selected_modems': ['mts|*|off'],
        'dpi': 'any',
        'core': 'stable',
    }
    with pytest.raises(RequestBuildError, match=str(MAX_CONFIGS_PER_TEST)):
        build_vless_request(links * 7, [], 'on', '')
    with pytest.raises(RequestBuildError):
        build_vless_request([_t('a.example', 443, None)], [], 'on', '')
    with pytest.raises(RequestBuildError):
        build_vless_request([], [], 'on', '')


def test_scan_request() -> None:
    cidr = _t('192.0.2.0', None, None, kind='cidr')
    body = build_scan_request(cidr, ['dobro|цфо|on'], 'on', {'icmp': True, 'tcp': True, 'sni': False}, [])
    assert body == {
        'cidr': '192.0.2.0',
        'operators': ['dobro|цфо|on'],
        'probes': {'icmp': True, 'tcp': True, 'sni': False},
        'dpi': 'on',
    }
    with_sni = build_scan_request(cidr, [], 'on', {'tcp': True, 'sni': True}, ['whitelisted.example'])
    assert with_sni['sni_hosts'] == ['whitelisted.example']
    with pytest.raises(RequestBuildError):
        build_scan_request(cidr, [], 'on', {'sni': True}, [])
    with pytest.raises(RequestBuildError):
        build_scan_request(_t('a.example', None, None), [], 'on', {'tcp': True}, [])


def test_probe_request_limits_targets_to_api_maximum() -> None:
    targets = [_t(f'h{i}.example', 443, None) for i in range(MAX_PROBE_TARGETS + 1)]
    with pytest.raises(RequestBuildError, match=str(MAX_PROBE_TARGETS)):
        build_probe_request(targets, [], 'on', {'tcp': True})
    assert (
        len(build_probe_request(targets[:MAX_PROBE_TARGETS], [], 'on', {'tcp': True})['targets']) == MAX_PROBE_TARGETS
    )
