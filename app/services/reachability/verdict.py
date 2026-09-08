"""Вердикт лега и его соответствие ожиданию.

Правила выведены из живых ответов (спец, разделы 3 и 6.4). Для хостов под Белый
список решает SNI-проба с настоящим SNI хоста: cert-validated TCP у Reality
проверяет сертификат dest и даёт ложный «blocked».
"""

from __future__ import annotations

from app.services.reachability.targets import PURPOSE_BS, PURPOSE_REGULAR


REACHABLE = 'reachable'
BLOCKED = 'blocked'
DOWN = 'down'
UNKNOWN = 'unknown'
CANCELLED = 'cancelled'


def _sni_name(entry: dict) -> str:
    """Имя SNI-записи. У API две формы: ``host`` либо ``evidence.sni`` (проба по флоту)."""
    evidence = entry.get('evidence') or {}
    return str(entry.get('host') or evidence.get('sni') or '').lower()


def _sni_alive(entry: dict) -> bool:
    return bool(entry.get('ok')) or entry.get('verdict') == 'alive'


def _aggregate_sni(entries: list[dict]) -> dict:
    """Несколько своих имён (Multi-SNI): хоть одно прошло — жив; все режутся — заблокирован."""
    alive = [entry for entry in entries if _sni_alive(entry)]
    if alive:
        return alive[0]
    blocked = [entry for entry in entries if entry.get('verdict') in ('blocked', 'refused')]
    return blocked[0] if blocked else entries[0]


def _pick_sni(entries: list[dict] | None, sni_host: str | None) -> dict | None:
    if not entries:
        return None
    if sni_host:
        for entry in entries:
            if _sni_name(entry) == sni_host.lower():
                return entry
    # Запись одна — это и есть наша проба, даже если исполнитель записал в неё имя хоста;
    # записей несколько и ни одна не про SNI хоста — это свои имена, судим по совокупности.
    return entries[0] if len(entries) == 1 else _aggregate_sni(entries)


def probe_leg_verdict(leg: dict, *, sni_host: str | None = None, reality: bool = False) -> str:
    if not leg.get('ok'):
        return UNKNOWN

    sni = _pick_sni(leg.get('sni'), sni_host)
    if sni is not None:
        if sni.get('ok') or sni.get('verdict') == 'alive':
            return REACHABLE
        if sni.get('verdict') in ('blocked', 'refused'):
            return BLOCKED

    tcp = leg.get('tcp')
    if tcp:
        if tcp.get('ok'):
            return REACHABLE
        verdict = tcp.get('verdict')
        if verdict == 'refused':
            return BLOCKED
        if verdict == 'blocked':
            if leg.get('tcp_is_tls') and reality:
                return DOWN if sni is not None else UNKNOWN
            return BLOCKED
        return DOWN

    icmp = leg.get('icmp')
    if icmp:
        return REACHABLE if icmp.get('ok') else DOWN
    return UNKNOWN


def compact_probe_verdict(result: dict | None) -> str:
    """Вердикт по компактной ячейке частичного результата (409): SNI → TCP → ICMP, как у полной формы."""
    if not result:
        return UNKNOWN
    sni = result.get('sni')
    if isinstance(sni, dict) and sni.get('ok'):
        return REACHABLE
    tcp = result.get('tcp')
    if isinstance(tcp, dict) and tcp.get('ok'):
        return REACHABLE
    if isinstance(sni, dict):
        return BLOCKED
    if isinstance(tcp, dict):
        return DOWN
    icmp = result.get('icmp')
    if isinstance(icmp, dict):
        return REACHABLE if icmp.get('ok') else DOWN
    return UNKNOWN


def vless_leg_verdict(leg: dict) -> str:
    if leg.get('cancelled') or leg.get('stage') == 'cancelled':
        return CANCELLED
    targets = leg.get('targets') or []
    any_target_ok = any(target.get('ok') for target in targets)
    if leg.get('ok') and leg.get('tunnel_up') and (any_target_ok or not targets):
        return REACHABLE
    if leg.get('tunnel_up'):
        return BLOCKED
    if leg.get('tcp_ok') is False:
        return DOWN
    return UNKNOWN


def matches_expectation(verdict: str, purpose: str, dpi: str) -> bool | None:
    """True/False, когда ожидание есть; None — справочная строка без ожидания."""
    if verdict == CANCELLED:
        return None
    expected_dpi = {PURPOSE_BS: 'on', PURPOSE_REGULAR: 'off'}.get(purpose)
    if expected_dpi is None or dpi != expected_dpi:
        return None
    return verdict == REACHABLE
