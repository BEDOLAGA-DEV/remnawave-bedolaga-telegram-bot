"""Цели проверки: нормализация ввода, ключ цели, назначение, /24, связь хост → нода."""

from __future__ import annotations

import pytest

from app.external.remnawave_api import RemnaWaveHost
from app.services.reachability.targets import (
    PURPOSE_BS,
    PURPOSE_REGULAR,
    Target,
    TargetValidationError,
    cidr24_for_ip,
    guess_purpose,
    hosts_for_node,
    is_reality_like,
    normalize_custom_target,
    probe_api_target,
    target_key,
    validate_cidr24,
)


@pytest.mark.parametrize(
    ('value', 'address', 'port'),
    [
        ('BS-Host.Example:9443', 'bs-host.example', 9443),
        ('eu-host.example', 'eu-host.example', None),
        ('https://eu-host.example/', 'eu-host.example', 443),
        ('http://eu-host.example:8080/path', 'eu-host.example', 8080),
        ('8.8.8.8:443', '8.8.8.8', 443),
        ('  1.1.1.1  ', '1.1.1.1', None),
        ('[2606:4700:4700::1111]:443', '2606:4700:4700::1111', 443),
    ],
)
def test_normalize_custom_target(value: str, address: str, port: int | None) -> None:
    target = normalize_custom_target(value)
    assert (target.kind, target.address, target.port) == ('custom', address, port)
    assert target.target_key == target_key(address, port)


@pytest.mark.parametrize(
    'value',
    [
        '127.0.0.1',
        '10.0.0.1:443',
        'localhost',
        '169.254.1.1',
        '224.0.0.1',
        '0.0.0.0',
        '192.0.2.1',
        'host:99999',
        'host:0',
        '',
        'a b.example',
        'vless://x@y:1',
    ],
)
def test_normalize_rejects_private_and_malformed(value: str) -> None:
    with pytest.raises(TargetValidationError):
        normalize_custom_target(value)


def test_target_key_is_lowercase_with_optional_port() -> None:
    assert target_key('BS-Host.Example', 9443) == 'bs-host.example:9443'
    assert target_key('EU.example', None) == 'eu.example'


def test_probe_api_target_keeps_port() -> None:
    assert probe_api_target(normalize_custom_target('bs-host.example:9443')) == 'bs-host.example:9443'
    assert probe_api_target(normalize_custom_target('eu-host.example')) == 'eu-host.example'


@pytest.mark.parametrize(
    ('address', 'sni', 'expected'),
    [
        ('bs-host.example', 'whitelisted.example', True),
        ('eu-host.example', 'eu-host.example', False),
        ('eu-host.example', 'cdn.eu-host.example', False),
        ('eu-host.example', None, False),
        ('192.0.2.1', 'whitelisted.example', True),
    ],
)
def test_is_reality_like(address: str, sni: str | None, expected: bool) -> None:
    assert is_reality_like(address, sni) is expected


@pytest.mark.parametrize(
    ('kwargs', 'expected'),
    [
        ({'address': 'bs-host.example', 'sni': 'whitelisted.example'}, PURPOSE_BS),
        ({'address': 'eu-host.example', 'sni': 'eu-host.example', 'remark': '🇩🇪 Germany'}, PURPOSE_REGULAR),
        ({'address': 'eu-host.example', 'sni': 'eu-host.example', 'remark': 'Russia | LTE | БС'}, PURPOSE_BS),
        ({'address': 'eu-host.example', 'sni': None, 'tag': 'BS'}, PURPOSE_BS),
    ],
)
def test_guess_purpose(kwargs: dict, expected: str) -> None:
    assert guess_purpose(**kwargs) == expected


def test_cidr_helpers() -> None:
    """Документационные диапазоны (192.0.2.0/24 и т. п.) не глобальные — их тоже режем."""
    assert validate_cidr24('8.8.8.0/24') == '8.8.8.0/24'
    assert validate_cidr24('8.8.8.77/24') == '8.8.8.0/24'
    assert cidr24_for_ip('8.8.8.142') == '8.8.8.0/24'
    for bad in ('8.8.8.0/23', '8.8.8.0/25', '10.0.0.0/24', '192.0.2.0/24', 'nope', '2001:db8::/24'):
        with pytest.raises(TargetValidationError):
            validate_cidr24(bad)


def _host(uuid: str, address: str, inbound: str | None) -> RemnaWaveHost:
    return RemnaWaveHost(uuid=uuid, remark=uuid, address=address, port=443, config_profile_inbound_uuid=inbound)


def test_hosts_for_node_matches_by_inbound_then_by_address() -> None:
    hosts = [_host('a', 'a.example', 'in-1'), _host('b', 'b.example', 'in-9'), _host('c', '192.0.2.5', None)]
    matched = hosts_for_node(hosts, node_active_inbounds=['in-1'], node_address='192.0.2.5', node_ips=[])
    assert [h.uuid for h in matched] == ['a', 'c']
    matched = hosts_for_node(hosts, node_active_inbounds=[], node_address='x', node_ips=['192.0.2.5'])
    assert [h.uuid for h in matched] == ['c']


def test_target_round_trips_through_dict() -> None:
    target = normalize_custom_target('bs-host.example:9443')
    assert Target.from_dict(target.as_dict()) == target
