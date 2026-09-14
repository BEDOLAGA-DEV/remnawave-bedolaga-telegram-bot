"""Ответы API раскладываются в леги с вердиктом и ожиданием; пропуски сливаются без мутаций."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.reachability.legs import build_probe_legs, build_vless_legs, merge_skipped, vless_op_key
from app.services.reachability.targets import Target
from tests.fixtures.bschek_fixtures import load_bschek_fixture


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
BS = Target(
    kind='host',
    label='BS',
    address='bs-host.example',
    port=9443,
    target_key='bs-host.example:9443',
    sni='whitelisted.example',
    ref={'host_uuid': 'h-bs'},
    purpose='bs',
).as_dict()
EU = Target(
    kind='host',
    label='EU',
    address='eu-host.example',
    port=None,
    target_key='eu-host.example',
    sni='eu-host.example',
    ref={'host_uuid': 'h-eu'},
    purpose='regular',
).as_dict()


def test_probe_legs_from_recorded_full_response() -> None:
    fx = load_bschek_fixture('p2_replay')
    legs = build_probe_legs([EU, BS], fx['request'], fx['body'], checked_at=NOW)
    assert len(legs) == 10
    by = {(leg['target_key'], leg['op_key']): leg for leg in legs}
    tele2_bs = by[('bs-host.example:9443', 'tele2|цфо|on')]
    assert (tele2_bs['verdict'], tele2_bs['matches_expectation'], tele2_bs['target_ref'], tele2_bs['dpi']) == (
        'reachable',
        True,
        'h-bs',
        'on',
    )
    assert (tele2_bs['target_kind'], tele2_bs['operator'], tele2_bs['region']) == ('host', 'tele2', 'ЦФО')
    tmobile_bs = by[('bs-host.example:9443', 't-mobile|цфо|on')]
    assert (tmobile_bs['verdict'], tmobile_bs['matches_expectation']) == ('down', False)
    eu_tele2 = by[('eu-host.example', 'tele2|цфо|on')]
    # Обычный хост под Белым списком — справочная строка, ожидания нет.
    assert (eu_tele2['verdict'], eu_tele2['matches_expectation']) == ('blocked', None)
    assert all(leg['checked_at'] == NOW and leg['kind'] == 'probe' and leg['raw'] for leg in legs)


def test_probe_legs_for_unknown_target_fall_back_to_api_key() -> None:
    fx = load_bschek_fixture('p1_probe')
    legs = build_probe_legs([], fx['request'], fx['body'], checked_at=NOW)
    assert len(legs) == 1
    assert (legs[0]['target_key'], legs[0]['target_kind'], legs[0]['target_ref']) == ('eu-host.example', 'custom', None)
    assert (legs[0]['verdict'], legs[0]['matches_expectation']) == ('down', None)


def test_probe_legs_match_api_target_case_insensitively() -> None:
    body = {'by_target': {'BS-HOST.example:9443': {'by_operator': {'mts|цфо|on': {'ok': True, 'dpi': 'on'}}}}}
    legs = build_probe_legs([BS], {}, body, checked_at=NOW)
    assert (legs[0]['target_key'], legs[0]['target_ref'], legs[0]['verdict']) == (
        'bs-host.example:9443',
        'h-bs',
        'unknown',
    )


def test_vless_legs_match_by_server_addr_and_compose_op_key() -> None:
    fx = load_bschek_fixture('v1_poll_12')
    legs = build_vless_legs([BS], fx['body']['result'], checked_at=NOW)
    assert [leg['op_key'] for leg in legs] == ['tele2|цфо|on', 'dobro|цфо|on']
    assert all(
        leg['verdict'] == 'reachable' and leg['matches_expectation'] is True and leg['target_ref'] == 'h-bs'
        for leg in legs
    )
    assert all(leg['kind'] == 'vless' and leg['dpi'] == 'on' and leg['target_kind'] == 'host' for leg in legs)


def test_vless_legs_fall_back_to_server_name_then_raw_address() -> None:
    raw = {
        'server_addr': 'other.example:443',
        'server_name': 'BS',
        'operator': 'mts',
        'region': 'ЦФО',
        'channel_state': 'DPI_ON',
    }
    by_name = build_vless_legs([BS], [raw], checked_at=NOW)[0]
    assert (by_name['target_key'], by_name['target_ref']) == ('bs-host.example:9443', 'h-bs')
    unknown = build_vless_legs([], [{**raw, 'server_name': 'x', 'channel_state': 'DOWN'}], checked_at=NOW)[0]
    assert (unknown['target_key'], unknown['target_kind'], unknown['dpi'], unknown['op_key']) == (
        'other.example:443',
        'custom',
        None,
        'mts|цфо|?',
    )


def test_vless_op_key_from_leg_fields() -> None:
    assert vless_op_key({'operator': 'mts', 'region': 'ЦФО', 'channel_state': 'DPI_OFF'}) == 'mts|цфо|off'
    assert vless_op_key({'operator': 'mts', 'region': 'ЦФО', 'channel_state': 'DOWN'}) == 'mts|цфо|?'
    assert vless_op_key({}) == '?|?|?'


def test_merge_skipped_keeps_ours_and_adds_api_lists_without_mutation() -> None:
    ours = {'dpi_off': [{'op_key': 'a'}], 'unavailable': [], 'unknown': ['x'], 'blocked_targets': []}
    response = {
        'skipped_dpi_off': [{'op_key': 'b'}],
        'skipped_unavailable': [{'op_key': 'c'}],
        'skipped': [{'target': '10.0.0.1'}],
    }
    merged = merge_skipped(ours, response)
    assert merged == {
        'dpi_off': [{'op_key': 'a'}, {'op_key': 'b'}],
        'unavailable': [{'op_key': 'c'}],
        'unknown': ['x'],
        'blocked_targets': [{'target': '10.0.0.1'}],
    }
    assert ours['dpi_off'] == [{'op_key': 'a'}]
    assert merge_skipped(None, {}) == {'dpi_off': [], 'unavailable': [], 'unknown': [], 'blocked_targets': []}
