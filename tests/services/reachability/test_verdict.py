"""Вердикт лега — чистая функция на записанных легах живого API.

Ключевые случаи: Reality-хост даёт ложный «blocked» на cert-validated TCP (решает
SNI-проба с настоящим SNI); у одной симки TCP проходит, а SNI режется; провал VLESS
с поднятым туннелем — это «режется», без TCP — «недоступен».
"""

from __future__ import annotations

import pytest

from app.services.reachability.verdict import (
    BLOCKED,
    CANCELLED,
    DOWN,
    REACHABLE,
    UNKNOWN,
    matches_expectation,
    probe_leg_verdict,
    vless_leg_verdict,
)
from tests.fixtures.bschek_fixtures import load_bschek_fixture


def _probe_leg(fixture: str, target: str, op_key: str) -> dict:
    return load_bschek_fixture(fixture)['body']['by_target'][target]['by_operator'][op_key]


BS = 'bs-host.example:9443'
EU = 'eu-host.example'
WL = 'whitelisted.example'


@pytest.mark.parametrize(
    ('fixture', 'target', 'op_key', 'sni_host', 'reality', 'expected'),
    [
        ('p2_replay', BS, 'tele2|цфо|on', WL, True, REACHABLE),  # tcp blocked, sni alive
        ('p2_replay', BS, 'sberm|цфо|on', WL, True, REACHABLE),  # чужое sni режется, своё живо
        ('p2_replay', BS, 't-mobile|цфо|on', WL, True, DOWN),  # сам IP недоступен
        ('p2_replay', EU, 'tele2|цфо|on', EU, False, BLOCKED),  # handshake timeout
        ('p2_replay', EU, 'sberm|цфо|on', EU, False, BLOCKED),  # refused
        ('p2_replay', EU, 'dobro|цфо|on', EU, False, DOWN),
        ('pF_replay_late', BS, 'yota|цфо|on', WL, True, BLOCKED),  # tcp ok, sni blocked
        ('pF_replay_late', BS, 'rtk|пфо|on', WL, True, REACHABLE),
        ('pF_replay_late', BS, 'mts|пфо|on', WL, True, DOWN),
        ('p1_probe', EU, 'mts|пфо|on', None, False, DOWN),  # tls verdict down, без sni
        ('p4_bare_mts_any', '1.1.1.1', 'mts|цфо|off', None, False, REACHABLE),
        ('p4_bare_mts_any', '1.1.1.1', 'mts|дфо|off', None, False, REACHABLE),
        ('p4_bare_mts_any', '1.1.1.1', 'mts|пфо|on', None, False, DOWN),
    ],
)
def test_probe_leg_verdict_on_recorded_legs(fixture, target, op_key, sni_host, reality, expected) -> None:
    assert probe_leg_verdict(_probe_leg(fixture, target, op_key), sni_host=sni_host, reality=reality) == expected


def test_reality_tls_blocked_without_sni_probe_is_unknown() -> None:
    leg = {
        'ok': True,
        'tcp_is_tls': True,
        'tcp': {'ok': False, 'verdict': 'blocked', 'cert_names': ['CN=*.whitelisted.example']},
        'sni': None,
    }
    assert probe_leg_verdict(leg, reality=True) == UNKNOWN
    assert probe_leg_verdict(leg, reality=False) == BLOCKED


def test_sni_entry_in_evidence_shape_is_understood() -> None:
    """Проба по флоту отдаёт sni[] без поля host: имя лежит в evidence.sni, форма другая."""
    leg = {
        'ok': True,
        'tcp_is_tls': False,
        'tcp': {'ok': True, 'received': 3, 'total': 3, 'error': ''},
        'sni': [
            {
                'ok': False,
                'kind': 'sni',
                'detail': 'handshake: Read timed out',
                'rtt_ms': 3580,
                'verdict': 'blocked',
                'evidence': {'sni': 'bs-host.example', 'phase': 'handshake'},
            }
        ],
    }
    assert probe_leg_verdict(leg, sni_host='whitelisted.example', reality=True) == BLOCKED
    # Записей несколько и ни одна не опознана — SNI не учитываем, решает TCP (он прошёл).
    two = {**leg, 'sni': [leg['sni'][0], {'ok': True, 'host': 'other.example', 'verdict': 'alive'}]}
    assert probe_leg_verdict(two, sni_host='whitelisted.example', reality=True) == REACHABLE


def test_probe_leg_not_executed_is_unknown() -> None:
    assert probe_leg_verdict({'ok': False, 'error': 'modem lost'}) == UNKNOWN


def test_icmp_only_probe() -> None:
    assert probe_leg_verdict({'ok': True, 'icmp': {'ok': True}, 'tcp': None, 'sni': None}) == REACHABLE
    assert probe_leg_verdict({'ok': True, 'icmp': {'ok': False}, 'tcp': None, 'sni': None}) == DOWN


def _vless_leg(fixture: str, index: int = 0) -> dict:
    return load_bschek_fixture(fixture)['body']['result'][index]


def test_vless_verdicts_on_recorded_legs() -> None:
    assert vless_leg_verdict(_vless_leg('v1_poll_12')) == REACHABLE
    zombie = _vless_leg('vB_poll_34')
    assert zombie['fail_reason'] == 'zombie_tcp'
    assert vless_leg_verdict(zombie) == BLOCKED
    assert vless_leg_verdict(_vless_leg('vC_after_cancel')) == CANCELLED


def test_vless_other_protocol_fail_reasons() -> None:
    legs = {leg['protocol']: leg for leg in load_bschek_fixture('vD_poll_19')['body']['result']}
    assert vless_leg_verdict(legs['vmess']) == DOWN  # tcp_timeout
    assert vless_leg_verdict(legs['hysteria2']) == BLOCKED  # dataplane_dead, tunnel_up


@pytest.mark.parametrize(
    ('verdict', 'purpose', 'dpi', 'expected'),
    [
        (REACHABLE, 'bs', 'on', True),
        (BLOCKED, 'bs', 'on', False),
        (DOWN, 'bs', 'on', False),
        (UNKNOWN, 'bs', 'on', False),
        (REACHABLE, 'bs', 'off', None),
        (REACHABLE, 'regular', 'off', True),
        (BLOCKED, 'regular', 'off', False),
        (BLOCKED, 'regular', 'on', None),
        (REACHABLE, 'unknown', 'on', None),
        (CANCELLED, 'bs', 'on', None),
    ],
)
def test_matches_expectation(verdict, purpose, dpi, expected) -> None:
    assert matches_expectation(verdict, purpose, dpi) is expected


def test_explicit_sni_names_any_alive_is_reachable_all_blocked_is_blocked() -> None:
    """Multi-SNI по своим именам: ни одно не совпадает с SNI хоста — судим по совокупности."""
    base = {'ok': True, 'tcp_is_tls': True, 'tcp': {'ok': False, 'verdict': 'blocked'}}
    mixed = {**base, 'sni': [{'host': 'ads.x5.ru', 'ok': False, 'verdict': 'blocked'}, {'host': 'vk.com', 'ok': True}]}
    assert probe_leg_verdict(mixed, sni_host='whitelisted.example', reality=True) == REACHABLE
    all_blocked = {
        **base,
        'sni': [
            {'host': 'ads.x5.ru', 'ok': False, 'verdict': 'blocked'},
            {'host': 'vk.com', 'ok': False, 'verdict': 'blocked'},
        ],
    }
    assert probe_leg_verdict(all_blocked, sni_host='whitelisted.example', reality=True) == BLOCKED
