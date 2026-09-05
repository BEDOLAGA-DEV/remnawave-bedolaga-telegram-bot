"""Матрица сводки: хосты панели в её порядке, исключённые скрыты, пропавшие цели — в конце."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.external.remnawave_api import RemnaWaveHost
from app.services.reachability.resolver import HostView, target_from_host
from app.services.reachability.summary import build_summary_rows


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _view(uuid: str, address: str, purpose: str, excluded: bool = False, guessed: bool = True) -> HostView:
    host = RemnaWaveHost(uuid=uuid, remark=uuid.upper(), address=address, port=443)
    return HostView(
        host=host, target=target_from_host(host, purpose), purpose_guessed=guessed, excluded=excluded, node_uuids=[]
    )


def _leg(target_key: str, ref: str | None, op_key: str, verdict: str, kind: str = 'host'):
    return SimpleNamespace(
        target_key=target_key,
        target_kind=kind,
        target_ref=ref,
        op_key=op_key,
        verdict=verdict,
        matches_expectation=verdict == 'reachable',
        checked_at=NOW,
        job_id=1,
    )


def test_rows_follow_panel_order_and_carry_purpose_and_cells() -> None:
    hosts = [_view('h-bs', 'bs.example', 'bs'), _view('h-eu', 'eu.example', 'regular', guessed=False)]
    legs = [
        _leg('eu.example:443', 'h-eu', 'mts|цфо|off', 'reachable'),
        _leg('bs.example:443', 'h-bs', 'mts|цфо|on', 'blocked'),
    ]
    rows = build_summary_rows(legs, hosts, {})
    assert [row['target_key'] for row in rows] == ['bs.example:443', 'eu.example:443']
    assert (rows[0]['purpose'], rows[0]['purpose_guessed'], rows[0]['label'], rows[0]['in_panel']) == (
        'bs',
        True,
        'H-BS',
        True,
    )
    assert rows[0]['cells']['mts|цфо|on'] == {
        'verdict': 'blocked',
        'matches_expectation': False,
        'checked_at': NOW,
        'job_id': 1,
    }
    assert rows[1]['purpose_guessed'] is False


def test_excluded_hosts_are_hidden_even_if_they_have_legs() -> None:
    hosts = [_view('h-x', 'x.example', 'regular', excluded=True), _view('h-y', 'y.example', 'regular')]
    legs = [_leg('x.example:443', 'h-x', 'mts|цфо|on', 'reachable')]
    rows = build_summary_rows(legs, hosts, {('host', 'h-x'): ('regular', True)})
    assert [row['target_key'] for row in rows] == ['y.example:443']
    assert rows[0]['cells'] == {}


def test_legs_of_targets_missing_from_panel_go_last_with_pref_purpose() -> None:
    hosts = [_view('h-y', 'y.example', 'regular')]
    legs = [_leg('gone.example:443', 'h-gone', 'mts|цфо|on', 'down')]
    rows = build_summary_rows(legs, hosts, {('host', 'h-gone'): ('bs', False)})
    assert [(row['target_key'], row['in_panel'], row['purpose']) for row in rows] == [
        ('y.example:443', True, 'regular'),
        ('gone.example:443', False, 'bs'),
    ]
    assert build_summary_rows(legs, [], {})[0]['purpose'] == 'unknown'


def test_rows_are_new_objects_and_inputs_untouched() -> None:
    hosts = [_view('h-y', 'y.example', 'regular')]
    legs = [_leg('y.example:443', 'h-y', 'a|b|on', 'reachable')]
    first = build_summary_rows(legs, hosts, {})
    second = build_summary_rows(legs, hosts, {})
    assert first == second and first[0] is not second[0] and first[0]['cells'] is not second[0]['cells']
