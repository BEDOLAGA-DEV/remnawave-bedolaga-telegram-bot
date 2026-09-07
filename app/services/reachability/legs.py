"""Раскладка ответов API в леги (цель × симка) и слияние пропусков.

Лег — словарь под ``ReachabilityLeg(**leg)``. Цель ищется среди целей задачи; если API
вернул цель, которой в задаче нет, лег всё равно сохраняется с типом ``custom``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.reachability.targets import (
    KIND_CUSTOM,
    PURPOSE_BS,
    PURPOSE_UNKNOWN,
    Target,
    is_reality_like,
    probe_api_target,
)
from app.services.reachability.verdict import (
    compact_probe_verdict,
    matches_expectation,
    probe_leg_verdict,
    vless_leg_verdict,
)


_CHANNEL_TO_DPI = {'DPI_ON': 'on', 'DPI_OFF': 'off'}


@dataclass(frozen=True)
class _LegContext:
    kind: str
    checked_at: datetime
    target: Target | None
    fallback_key: str

    @property
    def purpose(self) -> str:
        return self.target.purpose if self.target else PURPOSE_UNKNOWN

    @property
    def target_ref(self) -> str | None:
        ref = self.target.ref if self.target else {}
        return ref.get('host_uuid') or ref.get('node_uuid') or ref.get('short_uuid')


def _leg(ctx: _LegContext, *, op_key: str, raw: dict, verdict: str, dpi: str | None) -> dict:
    return {
        'kind': ctx.kind,
        'target_key': ctx.target.target_key if ctx.target else ctx.fallback_key.lower(),
        'target_kind': ctx.target.kind if ctx.target else KIND_CUSTOM,
        'target_ref': ctx.target_ref,
        'op_key': op_key,
        'operator': raw.get('operator'),
        'region': raw.get('region'),
        'dpi': dpi,
        'verdict': verdict,
        'matches_expectation': matches_expectation(verdict, ctx.purpose, dpi or ''),
        'raw': raw,
        'checked_at': ctx.checked_at,
    }


def _targets_by_api_key(targets: list[dict]) -> dict[str, Target]:
    parsed = [Target.from_dict(item) for item in targets]
    return {probe_api_target(target): target for target in parsed}


def build_probe_legs(targets: list[dict], request: dict, result: dict, *, checked_at: datetime) -> list[dict]:
    """Леги probe: ``by_target[цель].by_operator[op_key]`` → по одному легу на пару."""
    by_api_target = _targets_by_api_key(targets)
    legs: list[dict] = []
    for api_target, payload in (result.get('by_target') or {}).items():
        target = by_api_target.get(str(api_target).lower())
        reality = target is not None and (target.purpose == PURPOSE_BS or is_reality_like(target.address, target.sni))
        ctx = _LegContext(kind='probe', checked_at=checked_at, target=target, fallback_key=str(api_target))
        for op_key, raw in (payload.get('by_operator') or {}).items():
            verdict = probe_leg_verdict(raw, sni_host=target.sni if target else None, reality=reality)
            legs.append(_leg(ctx, op_key=str(op_key), raw=raw, verdict=verdict, dpi=raw.get('dpi')))
    return legs


def vless_op_key(leg: dict) -> str:
    """У лега VLESS нет op_key — собираем ``оператор|округ|on/off`` из его полей."""
    dpi = _CHANNEL_TO_DPI.get(str(leg.get('channel_state') or ''), '?')
    return f'{leg.get("operator") or "?"}|{str(leg.get("region") or "?").lower()}|{dpi}'


def build_vless_legs(targets: list[dict], legs_raw: list[dict], *, checked_at: datetime) -> list[dict]:
    """Леги VLESS: сервер ищется по ``server_addr`` (= target_key), запасной путь — по имени."""
    parsed = [Target.from_dict(item) for item in targets]
    by_key = {target.target_key: target for target in parsed}
    by_label = {target.label: target for target in parsed}
    legs: list[dict] = []
    for raw in legs_raw:
        server_addr = str(raw.get('server_addr') or '')
        target = by_key.get(server_addr.lower()) or by_label.get(str(raw.get('server_name') or ''))
        op_key = vless_op_key(raw)
        dpi = op_key.rsplit('|', 1)[1]
        ctx = _LegContext(
            kind='vless',
            checked_at=checked_at,
            target=target,
            fallback_key=server_addr or str(raw.get('server_name') or ''),
        )
        legs.append(
            _leg(ctx, op_key=op_key, raw=raw, verdict=vless_leg_verdict(raw), dpi=dpi if dpi in ('on', 'off') else None)
        )
    return legs


def merge_skipped(existing: dict | None, response: dict[str, Any]) -> dict:
    """Наши пропуски (расчёт по каталогу) + пропуски из ответа API. Всегда новый словарь."""
    base = existing or {}
    return {
        'dpi_off': [*base.get('dpi_off', []), *(response.get('skipped_dpi_off') or [])],
        'unavailable': [*base.get('unavailable', []), *(response.get('skipped_unavailable') or [])],
        'unknown': list(base.get('unknown', [])),
        'blocked_targets': [*base.get('blocked_targets', []), *(response.get('skipped') or [])],
    }


_PARTIAL_CELLS = ('sni', 'tcp', 'icmp', 'http')


def _partial_latency(result: dict | None) -> int | None:
    for name in _PARTIAL_CELLS:
        cell = (result or {}).get(name)
        if isinstance(cell, dict) and cell.get('latency_ms') is not None:
            return int(cell['latency_ms'])
    return None


def partial_probe_progress(details: dict) -> dict[str, Any]:
    """Срез частичного результата пробы из 409 request_in_progress — для «проверяем…» в кабинете."""
    legs = []
    for raw in details.get('legs') or []:
        if not isinstance(raw, dict):
            continue
        done = raw.get('state') == 'done'
        result = raw.get('result') if isinstance(raw.get('result'), dict) else None
        legs.append(
            {
                'target': str(raw.get('target') or ''),
                'operator': raw.get('operator'),
                'region': raw.get('region'),
                'dpi': raw.get('dpi'),
                'state': str(raw.get('state') or 'queued'),
                'verdict': compact_probe_verdict(result) if done else None,
                'latency_ms': _partial_latency(result) if done else None,
            }
        )
    elapsed = details.get('elapsed_sec')
    return {
        'done': int(details.get('done') or 0),
        'total': int(details.get('total') or 0),
        'elapsed_sec': float(elapsed) if elapsed is not None else None,
        'legs': legs,
    }
