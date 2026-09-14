"""Матрица сводки «цель × симка» из последних легов.

Строки — хосты панели в её порядке (исключённые скрыты), назначение берётся из
резолвера (решение админа или догадка). Цели, которых в панели уже нет, идут в конце
с назначением из предпочтений, а без него — «unknown».
"""

from __future__ import annotations

from typing import Any

from app.database.models import ReachabilityLeg
from app.services.reachability.resolver import HostView, PrefsMap
from app.services.reachability.targets import KIND_HOST, PURPOSE_UNKNOWN


def _cell(leg: ReachabilityLeg) -> dict[str, Any]:
    return {
        'verdict': leg.verdict,
        'matches_expectation': leg.matches_expectation,
        'checked_at': leg.checked_at,
        'job_id': leg.job_id,
    }


def _host_row(view: HostView) -> dict[str, Any]:
    return {
        'target_key': view.target.target_key,
        'kind': KIND_HOST,
        'ref': view.host.uuid,
        'label': view.target.label,
        'purpose': view.target.purpose,
        'purpose_guessed': view.purpose_guessed,
        'in_panel': True,
    }


def _leg_row(leg: ReachabilityLeg, prefs: PrefsMap) -> dict[str, Any]:
    purpose, _excluded = prefs.get((leg.target_kind or '', leg.target_ref or ''), (PURPOSE_UNKNOWN, False))
    return {
        'target_key': leg.target_key,
        'kind': leg.target_kind,
        'ref': leg.target_ref,
        'label': leg.target_key,
        'purpose': purpose,
        'purpose_guessed': False,
        'in_panel': False,
    }


def _excluded_refs(hosts: list[HostView], prefs: PrefsMap) -> set[str]:
    from_panel = {view.host.uuid for view in hosts if view.excluded}
    from_prefs = {ref for (_kind, ref), (_purpose, excluded) in prefs.items() if excluded}
    return from_panel | from_prefs


def build_summary_rows(legs: list[ReachabilityLeg], hosts: list[HostView], prefs: PrefsMap) -> list[dict[str, Any]]:
    """Всегда новые словари: строки и ячейки не разделяются между вызовами."""
    excluded = _excluded_refs(hosts, prefs)
    rows: dict[str, dict[str, Any]] = {}
    for view in hosts:
        if not view.excluded and view.target.target_key not in rows:
            rows[view.target.target_key] = _host_row(view)
    cells: dict[str, dict[str, dict[str, Any]]] = {}
    for leg in legs:
        if leg.target_ref in excluded:
            continue
        if leg.target_key not in rows:
            rows[leg.target_key] = _leg_row(leg, prefs)
        cells[leg.target_key] = {**cells.get(leg.target_key, {}), leg.op_key: _cell(leg)}
    return [{**row, 'cells': cells.get(key, {})} for key, row in rows.items()]
